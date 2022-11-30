import logging
import re

from reconcile.utils.vault import (
    VaultClient,
    _VaultClient,
    SecretVersionNotFound,
    SecretNotFound,
    SecretAccessForbidden,
)
from reconcile.gql_definitions.jenkins_configs import jenkins_configs
from reconcile.gql_definitions.jenkins_configs.jenkins_configs import (
    JenkinsConfigV1_JenkinsConfigV1,
    JenkinsConfigsQueryData,
)

from reconcile.gql_definitions.vault_policies.vault_policies import (
    VaultPoliciesQueryData,
)

from reconcile.gql_definitions.vault_policies import vault_policies
from reconcile.gql_definitions.vault_instances import vault_instances
from reconcile.gql_definitions.vault_instances.vault_instances import (
    VaultInstanceAuthApproleV1,
    VaultInstanceV1,
    VaultReplicationConfigV1,
    VaultReplicationJenkinsV1,
    VaultReplicationConfigV1_VaultInstanceV1_VaultInstanceAuthV1_VaultInstanceAuthApproleV1,
    VaultReplicationConfigV1_VaultInstanceV1,
)

from reconcile.utils import gql
from typing import cast, Optional, Union
from collections.abc import Iterable

QONTRACT_INTEGRATION = "vault-replication"


class VaultInvalidPaths(Exception):
    pass


class VaultInvalidAuthMethod(Exception):
    pass


def deep_copy_versions(
    dry_run: bool,
    source_vault: _VaultClient,
    dest_vault: _VaultClient,
    current_dest_version: int,
    current_source_version: int,
    path: str,
) -> None:
    for version in range(current_dest_version + 1, current_source_version + 1):
        secret_dict = {"path": path, "version": version}
        secret, src_version = source_vault.read_all_with_version(secret_dict)
        write_dict = {"path": path, "data": secret}
        logging.info(["replicate_vault_secret", src_version, path])
        if not dry_run:
            dest_vault.write(secret=write_dict, decode_base64=False)


def copy_vault_secret(
    dry_run: bool, source_vault: _VaultClient, dest_vault: _VaultClient, path: str
) -> None:

    secret_dict = {"path": path, "version": "LATEST"}

    try:
        _, version = source_vault.read_all_with_version(secret_dict)
    except SecretAccessForbidden:
        raise SecretAccessForbidden("Cannot read secret from source vault")

    try:
        _, dest_version = dest_vault.read_all_with_version(secret_dict)
        if dest_version is None and version is None:
            # v1 secrets don't have version
            secret, _ = source_vault.read_all_with_version(secret_dict)
            write_dict = {"path": path, "data": secret}
            logging.info(["replicate_vault_secret", path])
            if not dry_run:
                dest_vault.write(secret=write_dict, decode_base64=False)
        elif dest_version < version:
            deep_copy_versions(
                dry_run=dry_run,
                source_vault=source_vault,
                dest_vault=dest_vault,
                current_dest_version=dest_version,
                current_source_version=version,
                path=path,
            )
    except (SecretVersionNotFound, SecretNotFound):
        logging.info(["replicate_vault_secret", "Secret not found", path])
        # Handle v1 secrets where version is None and we don't need to deep sync.
        if version is None:
            logging.info(["replicate_vault_secret", path])
            if not dry_run:
                secret, _ = source_vault.read_all_with_version(secret_dict)
                write_dict = {"path": path, "data": secret}
                dest_vault.write(secret=write_dict, decode_base64=False)
        else:
            deep_copy_versions(
                dry_run=dry_run,
                source_vault=source_vault,
                dest_vault=dest_vault,
                current_dest_version=0,
                current_source_version=version,
                path=path,
            )


def check_invalid_paths(
    path_list: Iterable[str],
    policy_paths: Optional[Iterable[str]],
) -> None:

    if policy_paths is not None:
        invalid_paths = list_invalid_paths(path_list, policy_paths)
        if invalid_paths:
            logging.error(["replicate_vault_secret", "Invalid paths", invalid_paths])
            raise VaultInvalidPaths


def list_invalid_paths(
    path_list: Iterable[str], policy_paths: Iterable[str]
) -> list[str]:
    invalid_paths = []

    for path in path_list:
        if not policy_contains_path(path, policy_paths):
            invalid_paths.append(path)

    return invalid_paths


def policy_contains_path(path: str, policy_paths: Iterable[str]) -> bool:
    return any(path in p_path for p_path in policy_paths)


def get_policy_paths(
    policy_name: str, instance_name: str, policy_query_data: VaultPoliciesQueryData
) -> list[str]:
    # query_data = vault_policies.query(query_func=gql.get_api().query)
    policy_paths = []

    if policy_query_data.policy:
        for policy in policy_query_data.policy:
            if policy.name == policy_name and policy.instance.name == instance_name:
                for line in policy.rules.split("\n"):
                    res = re.search(r"path \s*[\'\"](.+)[\'\"]", line)

                    if res is not None:
                        policy_paths.append(res.group(1))

    return policy_paths


def get_jenkins_secret_list(
    vault_instance: _VaultClient,
    jenkins_instance: str,
    query_data: JenkinsConfigsQueryData,
) -> list[str]:
    """Returns a list of secrets used in a jenkins instance

    The input secret is the name of a jenkins instance to filter
    the secrets:
    * jenkins_instance - Jenkins instance name
    """
    secret_list = []

    if query_data.jenkins_configs:
        for p in query_data.jenkins_configs:
            if (
                isinstance(p, JenkinsConfigV1_JenkinsConfigV1)
                and p.instance.name == jenkins_instance
                and p.config_path
            ):
                secret_paths = [
                    line
                    for line in p.config_path.content.split("\n")
                    if "secret-path" in line
                ]
                for line in secret_paths:
                    res = re.search(r"secret-path:\s*[\'\"](.+)[\'\"]", line)
                    if res is not None:
                        secret_path = res.group(1)
                        if "{" in secret_path:
                            wildcard_list = process_wildcard_paths_with_text(
                                vault_instance, secret_path
                            )
                            secret_list.extend(wildcard_list)
                        else:
                            secret_list.append(secret_path)

    return secret_list


def get_vault_credentials(
    vault_instance: Union[VaultInstanceV1, VaultReplicationConfigV1_VaultInstanceV1]
) -> dict[str, Optional[str]]:
    """Returns a dictionary with the credentials used to authenticate with Vault,
    retrieved from the values present on AppInterface.

    The input is a VaultInstanceV1 object, that contains secret references to be retreived
    from vault. The output is a dictionary with the credentials used to authenticate with
    Vault.
    * vault_instance - VaultInstanceV1 object from AppInterface Data.
    """
    vault_creds = {}
    vault = cast(_VaultClient, VaultClient())

    if not isinstance(
        vault_instance.auth, VaultInstanceAuthApproleV1
    ) and not isinstance(
        vault_instance.auth,
        VaultReplicationConfigV1_VaultInstanceV1_VaultInstanceAuthV1_VaultInstanceAuthApproleV1,
    ):
        raise VaultInvalidAuthMethod

    role_id = {
        "path": vault_instance.auth.role_id.path,
        "field": vault_instance.auth.role_id.field,
    }
    secret_id = {
        "path": vault_instance.auth.secret_id.path,
        "field": vault_instance.auth.secret_id.field,
    }

    vault_creds["role_id"] = vault.read(role_id)
    vault_creds["secret_id"] = vault.read(secret_id)
    vault_creds["server"] = vault_instance.address

    return vault_creds


def replicate_paths(
    dry_run: bool,
    source_vault: _VaultClient,
    dest_vault: _VaultClient,
    replications: VaultReplicationConfigV1,
) -> None:

    if replications.paths is None:
        return

    for path in replications.paths:

        if isinstance(path, VaultReplicationJenkinsV1):
            if path.policy is not None:
                vault_query_data = vault_policies.query(query_func=gql.get_api().query)
                policy_paths = get_policy_paths(
                    path.policy.name,
                    path.policy.instance.name,
                    vault_query_data,
                )
            else:
                policy_paths = None

            jenkins_query_data = jenkins_configs.query(query_func=gql.get_api().query)
            path_list = get_jenkins_secret_list(
                source_vault, path.jenkins_instance.name, jenkins_query_data
            )
            check_invalid_paths(path_list, policy_paths)
            for vault_path in path_list:
                copy_vault_secret(dry_run, source_vault, dest_vault, vault_path)


def get_start_end_secret(path: str) -> tuple[str, str]:

    start = path[0 : path.index("{")]
    if start[-1] != "/":
        start = start.rsplit("/", 1)[0] + "/"
    try:
        end = path[path[path.index("}") : :].index("/") + path.index("}") : :]
    except ValueError:
        end = ""

    return start, end


def process_wildcard_paths_with_text(
    vault_instance: _VaultClient, path: str
) -> list[str]:
    """Returns a list of secrets that match a wildcard path

    The input secret is the name of a jenkins instance to filter
    the secrets:
    * path - Vault path
    """

    secret_list = []
    path_slices = path.split("/")
    for s in path_slices:
        if "{" in s and "}" in s:
            wildcard = s

    cap_groups = re.search(r"(.*)(\{.*\})(.*)", wildcard)
    if cap_groups is not None:
        prefix = cap_groups.group(1)
        suffix = cap_groups.group(3)
    else:
        raise VaultInvalidPaths

    start, end = get_start_end_secret(path)
    vault_list = vault_instance.list(start)

    for secret in vault_list:
        if prefix in secret and suffix in secret:
            final = start + secret + end[1::]
            secret_list.append(final)

    return secret_list


def run(dry_run: bool) -> None:

    query_data = vault_instances.query(query_func=gql.get_api().query)

    if query_data.vault_instances:
        for instance in query_data.vault_instances:
            if instance.replication:
                for replication in instance.replication:
                    source_creds = get_vault_credentials(instance)
                    dest_creds = get_vault_credentials(replication.vault_instance)

                    # Private class _VaultClient is used because the public class is
                    # defined as a singleton, and we need to create multiple instances
                    # as the source vault is different than the replication.
                    source_vault = _VaultClient(
                        server=source_creds["server"],
                        role_id=source_creds["role_id"],
                        secret_id=source_creds["secret_id"],
                    )
                    dest_vault = _VaultClient(
                        server=dest_creds["server"],
                        role_id=dest_creds["role_id"],
                        secret_id=dest_creds["secret_id"],
                    )

                    replicate_paths(
                        dry_run=dry_run,
                        source_vault=source_vault,
                        dest_vault=dest_vault,
                        replications=replication,
                    )
