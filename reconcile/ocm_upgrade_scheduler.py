import copy
import logging
import sys
from collections.abc import Mapping
from datetime import datetime
from typing import (
    Any,
    Iterable,
    Optional,
)

from croniter import croniter
from semver import VersionInfo

from reconcile import queries
from reconcile.utils.cluster_version_data import (
    VersionData,
    WorkloadHistory,
    get_version_data,
)
from reconcile.utils.disabled_integrations import integration_is_enabled
from reconcile.utils.ocm import (
    OCM,
    OCM_PRODUCT_OSD,
    OCMMap,
    Sector,
)
from reconcile.utils.semver_helper import (
    parse_semver,
    sort_versions,
)
from reconcile.utils.state import init_state

QONTRACT_INTEGRATION = "ocm-upgrade-scheduler"

SUPPORTED_OCM_PRODUCTS = [OCM_PRODUCT_OSD]


# consider first lower versions and lower soakdays (when versions are equal)
def sort_key(d: dict) -> tuple:
    return (
        parse_semver(d["current_version"]),
        d["conditions"].get("soakDays") or 0,
    )


def fetch_current_state(
    clusters: list[dict[str, Any]], ocm_map: OCMMap, addons: bool = False
) -> list[dict[str, Any]]:
    current_state = []
    for cluster in clusters:
        cluster_name = cluster["name"]
        ocm = ocm_map.get(cluster_name)
        if addons:
            upgrade_policies = ocm.get_addon_upgrade_policies(cluster_name)
        else:
            upgrade_policies = ocm.get_upgrade_policies(cluster_name)
        for upgrade_policy in upgrade_policies:
            upgrade_policy["cluster"] = cluster_name
            current_state.append(upgrade_policy)

    return current_state


def fetch_desired_state(
    clusters: list[dict[str, Any]], ocm_map: OCMMap, addons: bool = False
) -> list[dict[str, Any]]:
    desired_state = []
    for cluster in clusters:
        cluster_name = cluster["name"]
        upgrade_policy = cluster["upgradePolicy"]
        upgrade_policy["cluster"] = cluster_name
        ocm: OCM = ocm_map.get(cluster_name)
        if not ocm.is_ready(cluster_name):
            # cluster has been deleted in OCM or is not ready yet
            continue
        # Replace sector names by their related OCM Sector object, including dependencies
        sector_name = upgrade_policy["conditions"].get("sector")
        if sector_name:
            upgrade_policy["conditions"]["sector"] = ocm.sectors[sector_name]

        if addons:
            cluster_addons = ocm.get_cluster_addons(cluster_name, with_version=True)
            for addon in cluster_addons:
                policy = copy.deepcopy(upgrade_policy)
                policy["addon_id"] = addon["id"]
                policy["current_version"] = addon["version"]
                desired_state.append(policy)
        else:
            spec = ocm.clusters[cluster_name].spec
            upgrade_policy["current_version"] = spec.version
            upgrade_policy["channel"] = spec.channel
            desired_state.append(upgrade_policy)

    sorted_desired_state = sorted(desired_state, key=sort_key)

    return sorted_desired_state


def update_history(version_data: VersionData, upgrade_policies: list[dict[str, Any]]):
    """Update history with information from clusters with upgrade policies.

    Args:
        version_data (VersionData): version data, including history of soakdays
        upgrade_policies (list): query results of clusters upgrade policies
    """
    now = datetime.utcnow()
    check_in = version_data.check_in or now

    # we iterate over clusters upgrade policies and update the version history
    for item in upgrade_policies:
        current_version = item["current_version"]
        cluster = item["cluster"]
        workloads = item["workloads"]
        # we keep the version history per workload
        for w in workloads:
            workload_history = version_data.workload_history(
                current_version, w, WorkloadHistory()
            )

            # if the cluster is already reporting - accumulate it.
            # if not - add it to the reporting list (first report)
            if cluster in workload_history.reporting:
                workload_history.soak_days += (
                    now - check_in
                ).total_seconds() / 86400  # seconds in day
            else:
                workload_history.reporting.append(cluster)

    version_data.update_stats(upgrade_policies)

    version_data.check_in = now


def get_version_data_map(
    dry_run: bool,
    upgrade_policies: list[dict[str, Any]],
    ocm_map: OCMMap,
    addon_id: str = "",
) -> dict[str, VersionData]:
    """Get a summary of versions history per OCM instance

    Args:
        dry_run (bool): save updated history to remote state
        upgrade_policies (list): query results of clusters upgrade policies
        ocm_map (OCMMap): OCM clients per OCM instance
        addon_id (str): optional addon id to get & store the addon specific state,
          additionally to the ocm org name

    Returns:
        dict: version data per OCM instance
    """
    state = init_state(integration=QONTRACT_INTEGRATION)
    results: dict[str, VersionData] = {}
    # we keep a remote state per OCM instance
    for ocm_name in ocm_map.instances():
        state_key = f"{ocm_name}/{addon_id}" if addon_id else ocm_name
        version_data = get_version_data(state, state_key)
        update_history(version_data, upgrade_policies)
        results[ocm_name] = version_data
        if not dry_run:
            version_data.save(state, state_key)

    # aggregate data from other ocm orgs
    # this is done *after* saving the state: we do not store the other orgs data in our state.
    for ocm_name in ocm_map.instances():
        ocm = ocm_map[ocm_name]
        for other_ocm in ocm.inheritVersionData:
            other_ocm_name = other_ocm["name"]
            if ocm_name == other_ocm_name:
                raise ValueError(
                    f"[{ocm_name}] OCM organization inherits version data from itself"
                )
            if ocm.name not in [
                o["name"] for o in other_ocm.get("publishVersionData") or []
            ]:
                raise ValueError(
                    f"[{ocm_name}] OCM organization inherits version data from {other_ocm_name}, but this data is not published to it: missing publishVersionData in {other_ocm_name}"
                )
            state_key = f"{other_ocm_name}/{addon_id}" if addon_id else other_ocm_name
            other_ocm_data = get_version_data(state, state_key)
            results[ocm_name].aggregate(other_ocm_data, other_ocm_name)

    return results


def workload_sector_versions(sector: Sector, workload: str) -> list[VersionInfo]:
    """
    get all versions of clusters running the specified workload in that sector
    """
    versions = []
    for cluster_info in sector.cluster_infos:
        # clusters within a sector always have workloads (mandatory in schema)
        workloads = cluster_info["upgradePolicy"]["workloads"]
        if workload in workloads:
            versions.append(
                parse_semver(sector.ocmspec(cluster_info["name"]).spec.version)
            )
    return versions


def workload_sector_dependencies(sector: Sector, workload: str) -> set[Sector]:
    """
    get the list of first dependency sectors with non-empty versions for that workload in the
    sector dependency tree. This goes down recursively through the dependency tree.
    """
    deps = set()
    for dep in sector.dependencies:
        if workload_sector_versions(dep, workload):
            deps.add(dep)
        else:
            deps.update(workload_sector_dependencies(dep, workload))
    return deps


def version_conditions_met(
    version: str,
    version_data_map: dict[str, VersionData],
    ocm_name: str,
    workloads: list[str],
    upgrade_conditions: dict[str, Any],
):
    """Check that upgrade conditions are met for a version

    Args:
        version (string): version to check
        history (dict): history of versions per OCM instance
        ocm_name (string): name of OCM instance
        upgrade_conditions (dict): query results of upgrade conditions
        workloads (list): strings representing types of workloads

    Returns:
        bool: are version upgrade conditions met
    """
    sector = upgrade_conditions.get("sector")
    if sector:
        version_data = version_data_map[ocm_name]
        # check that inherited orgs run at least that version for our workloads
        if not version_data.validate_against_inherited(version, workloads):
            return False

        # check if previous sectors run at least this version for that workload
        # we will check dependencies recursively until there are versions for the given workload
        # or no more dependencies to check
        for w in workloads:
            for dep in workload_sector_dependencies(sector, w):
                dep_versions = workload_sector_versions(dep, w)
                if not dep_versions:
                    continue
                if min(dep_versions) < parse_semver(version):
                    return False

    # check soak days condition is met for this version
    soak_days = upgrade_conditions.get("soakDays", None)
    if soak_days is not None:
        version_data = version_data_map[ocm_name]
        for w in workloads:
            workload_history = version_data.workload_history(version, w)
            if soak_days > workload_history.soak_days:
                return False

    return True


def gates_to_agree(
    version_prefix: str, cluster: str, cluster_version: str, ocm: OCM
) -> list[str]:
    """Check via OCM if a version is agreed

    Args:
        version_prefix (string): major.minor version prefix
        cluster (string)
        cluster_version (string): current version of the cluster
        ocm (OCM): used to fetch infos from OCM

    Returns:
        bool: true on missing agreement
    """
    agreements = {
        agreement["version_gate"]["id"]
        for agreement in ocm.get_version_agreement(cluster)
    }
    semver_cluster = parse_semver(f"{cluster_version}")

    return [
        gate["id"]
        for gate in ocm.get_version_gates(version_prefix)
        if gate["id"] not in agreements and semver_cluster.match(f"<{version_prefix}.0")
    ]


def get_version_prefix(version: str) -> str:
    semver = parse_semver(version)
    return f"{semver.major}.{semver.minor}"


def upgradeable_version(
    policy: Mapping,
    version_data_map: dict[str, VersionData],
    ocm: OCM,
    upgrades: Iterable[str],
    addon_id: str = "",
) -> Optional[str]:
    """Get the highest next version we can upgrade to, fulfilling all conditions"""
    for version in reversed(sort_versions(upgrades)):
        if addon_id and ocm.addon_version_blocked(version, addon_id):
            continue
        if not addon_id and ocm.version_blocked(version):
            continue
        if version_conditions_met(
            version,
            version_data_map,
            ocm.name,
            policy["workloads"],
            policy["conditions"],
        ):
            return version
    return None


def cluster_mutexes(policy: dict) -> list[str]:
    """List all mutex locks for the given cluster"""
    return (policy.get("conditions") or {}).get("mutexes") or []


def calculate_diff(
    current_state: list[dict[str, Any]],
    desired_state: list[dict[str, Any]],
    ocm_map: OCMMap,
    version_data_map: dict[str, VersionData],
    addon_id: str = "",
) -> list[Any]:
    """Check available upgrades for each cluster in the desired state
    according to upgrade conditions

    Args:
        current_state (list): current state of upgrade policies
        desired_state (list): desired state of upgrade policies
        ocm_map (OCMMap): OCM clients per OCM instance
        version_data_map (dict): version data history per OCM instance
        addon_id (str): optional addonid to calculate diffs for

    Returns:
        list: upgrade policies to be applied
    """
    diffs = []

    # all clusters with a current upgradePolicy are considered locked
    locked = {}
    for policy in desired_state:
        if policy["cluster"] in [s["cluster"] for s in current_state]:
            for mutex in cluster_mutexes(policy):
                locked[mutex] = policy["cluster"]

    now = datetime.utcnow()
    for d in desired_state:
        # ignore clusters with an existing upgrade policy
        cluster = d["cluster"]
        current_version = d["current_version"]
        ocm = ocm_map.get(cluster)
        c = [c for c in current_state if c["cluster"] == cluster]
        if c:
            # there can only be one upgrade policy per cluster
            if len(c) != 1:
                raise ValueError(f"[{cluster}] expected only one upgrade policy")
            current = c[0]
            version = current.get("version")  # may not exist in automatic upgrades
            if version and not addon_id and ocm.version_blocked(version):
                next_run = current.get("next_run")
                if next_run and datetime.strptime(next_run, "%Y-%m-%dT%H:%M:%SZ") < now:
                    logging.warning(
                        f"[{cluster}] currently upgrading to blocked version '{version}'"
                    )
                    continue
                logging.debug(
                    f"[{ocm.name}/{cluster}] found planned upgrade policy "
                    + f"with blocked version {version}"
                )
                item = {
                    "action": "delete",
                    "cluster": cluster,
                    "version": version,
                    "id": current["id"],
                }
                diffs.append(item)
            else:
                logging.debug(
                    f"[{ocm.name}/{cluster}] skipping cluster with existing upgrade policy"
                )
                continue

        schedule = d["schedule"]
        next_schedule_in_seconds = 0
        iter = croniter(schedule)
        # ClusterService refuses scheduling upgrades less than 5m in advance
        # Let's find the next schedule that is at least 5m ahead.
        # We do not need that much delay for addon upgrades since they run
        # immediately
        delay_minutes = 1 if addon_id else 5
        while next_schedule_in_seconds < delay_minutes * 60:
            next_schedule = iter.get_next(datetime)
            next_schedule_in_seconds = (next_schedule - now).total_seconds()
        next_schedule_in_hours = next_schedule_in_seconds / 3600  # seconds in hour

        # ignore clusters with an upgrade schedule not within the next 2 hours
        within_upgrade_timeframe = next_schedule_in_hours <= 2
        if addon_id:
            # addons upgrade cannot be scheduled in advance as the "next_run" field
            # is not supported. So we run this only 10min before schedule to be somewhat
            # correct
            within_upgrade_timeframe = next_schedule_in_seconds / 60 <= 10
        if not within_upgrade_timeframe:
            logging.debug(
                f"[{ocm.name}/{cluster}] skipping cluster with no upcoming upgrade"
            )
            continue

        if any(lock in locked for lock in cluster_mutexes(d)):
            locking = {
                lock: locked[lock] for lock in cluster_mutexes(d) if lock in locked
            }
            logging.debug(
                f"[{ocm.name}/{cluster}] skipping cluster: locked out by {locking}"
            )
            continue

        # choose version that meets the conditions and add it to the diffs
        if addon_id:
            # an alternative is to find available upgrades for our current version from
            # ${API_CLUSTERS_MGMT}/addons/${addon_id}/versions
            # .items[] | select(.id == {current_version}) | .available_upgrades
            # but we will always want to get the one that is currently published normally
            upgrades = [
                a["version"]["id"]
                for a in ocm.addons
                if a["id"] == addon_id and a["version"]["id"] != d["current_version"]
            ]
        else:
            upgrades = ocm.get_available_upgrades(d["current_version"], d["channel"])
        version = upgradeable_version(d, version_data_map, ocm, upgrades, addon_id)

        if version:
            item = {
                "action": "create",
                "cluster": cluster,
                "version": version,
                "schedule_type": "manual",
            }
            if addon_id:
                item["addon_id"] = addon_id
                item["cluster_id"] = ocm.cluster_ids[cluster]
                item["upgrade_type"] = "ADDON"
                # next_run is not supported by addons
            else:
                item["next_run"] = next_schedule.strftime("%Y-%m-%dT%H:%M:%SZ")
                item["gates_to_agree"] = gates_to_agree(
                    get_version_prefix(version),
                    cluster,
                    current_version,
                    ocm,
                )
            for mutex in cluster_mutexes(d):
                locked[mutex] = cluster
            diffs.append(item)

    return diffs


def sort_diffs(diff):
    if diff["action"] == "delete":
        return 1
    return 2


def action_log(*items: Optional[str]) -> None:
    # log all non-empty, non-null items
    logging.info([item for item in items if item])


def act(dry_run: bool, diffs: list[dict], ocm_map: OCMMap, addon_id: str = "") -> None:
    diffs.sort(key=sort_diffs)
    for diff in diffs:
        action = diff.pop("action")
        cluster = diff.pop("cluster")
        ocm = ocm_map.get(cluster)
        if action == "create":
            action_log(
                action,
                ocm.name,
                cluster,
                addon_id,
                diff["version"],
                diff.get("next_run"),
            )
            if not dry_run:
                if addon_id:
                    ocm.create_addon_upgrade_policy(cluster, diff)
                else:
                    gates_to_agree = diff.pop("gates_to_agree")
                    for gate in gates_to_agree:
                        action_log(
                            action,
                            ocm.name,
                            cluster,
                            addon_id,
                            diff["version"],
                            f"Creating version agreement for gate {gate}",
                        )
                        agreement = ocm.create_version_agreement(gate, cluster)
                        if agreement.get("version_gate") is None:
                            logging.error(
                                f"Unexpected response while creating version "
                                f"agreement with id {gate} for cluster {cluster}"
                            )
                    ocm.create_upgrade_policy(cluster, diff)
        elif action == "delete":
            action_log(action, ocm.name, cluster, addon_id, diff["version"])
            if not dry_run:
                if addon_id:
                    ocm.delete_addon_upgrade_policy(cluster, diff)
                else:
                    ocm.delete_upgrade_policy(cluster, diff)


def _cluster_is_compatible(cluster: Mapping[str, Any]) -> bool:
    return (
        cluster.get("ocm") is not None
        and cluster.get("upgradePolicy") is not None
        and cluster["spec"]["product"] in SUPPORTED_OCM_PRODUCTS
    )


def run(dry_run, gitlab_project_id=None, thread_pool_size=10):
    clusters = queries.get_clusters()
    settings = queries.get_app_interface_settings()

    clusters = [
        c
        for c in clusters
        if integration_is_enabled(QONTRACT_INTEGRATION, c) and _cluster_is_compatible(c)
    ]

    if not clusters:
        logging.debug("No upgradePolicy definitions found in app-interface")
        sys.exit(0)

    ocm_map = OCMMap(
        clusters=clusters,
        integration=QONTRACT_INTEGRATION,
        settings=settings,
        init_version_gates=True,
    )
    current_state = fetch_current_state(clusters, ocm_map)
    desired_state = fetch_desired_state(clusters, ocm_map)
    version_data_map = get_version_data_map(dry_run, desired_state, ocm_map)
    diffs = calculate_diff(current_state, desired_state, ocm_map, version_data_map)
    act(dry_run, diffs, ocm_map)
