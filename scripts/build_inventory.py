"""Build an Ansible inventory from configured HCP workspace outputs."""

import argparse
import ipaddress
import json
import os
import string
import subprocess
import sys
from pathlib import Path

from scripts.deployment_scope import changed_paths, terraform_changed
from scripts.hcp_deploy import HCP, ORGANIZATION, WORKSPACES, DeploymentError, preflight

ROLE_GROUPS = {"control-plane": "control_plane", "worker": "workers"}
DATA_DEVICES = {
    f"/dev/oracleoci/oraclevd{suffix}" for suffix in "bcdefghijklmnopqrstuvwxyz"
} | {f"/dev/oracleoci/oraclevda{suffix}" for suffix in "abcdef"}


def validate_volumes(volumes, context):
    if not isinstance(volumes, dict):
        raise DeploymentError(
            f"{context}: block_volumes must be a map; apply the updated Terraform outputs first."
        )
    devices = set()
    result = {}
    for name, volume in sorted(volumes.items()):
        if not isinstance(name, str) or not name or not isinstance(volume, dict):
            raise DeploymentError(
                f"{context}: each block volume needs a name and attachment details."
            )
        device = volume.get("device")
        if not isinstance(device, str) or device not in DATA_DEVICES:
            raise DeploymentError(
                f"{context}: {name} needs an explicit OCI data-device path in Terraform (for example /dev/oracleoci/oraclevdb)."
            )
        if device in devices:
            raise DeploymentError(f"{context}: multiple volumes use device {device}.")
        if (
            volume.get("attachment_type") != "paravirtualized"
            or volume.get("is_read_only") is not False
        ):
            raise DeploymentError(
                f"{context}: {name} must be a writable paravirtualized attachment for automatic preparation."
            )
        devices.add(device)
        result[name] = {
            "device": device,
            "attachment_type": "paravirtualized",
            "is_read_only": False,
        }
    return result


def fetch_outputs(client):
    outputs = {}
    for name in WORKSPACES:
        workspace = client.request(
            "GET", f"/organizations/{ORGANIZATION}/workspaces/{name}"
        )["data"]
        entries = list(
            client.items(f"/workspaces/{workspace['id']}/current-state-version-outputs")
        )
        inventory = [
            entry["attributes"]
            for entry in entries
            if entry["attributes"]["name"] == "inventory"
        ]
        if len(inventory) != 1:
            raise DeploymentError(
                f"{name}: missing inventory output. Apply the updated Terraform outputs first."
            )
        if inventory[0].get("sensitive") or not isinstance(
            inventory[0].get("value"), dict
        ):
            raise DeploymentError(
                f"{name}: inventory must be a non-sensitive object output."
            )
        outputs[name] = inventory[0]["value"]
    return outputs


def build_inventory(outputs, commit):
    if set(outputs) != set(WORKSPACES):
        raise DeploymentError(
            "Inventory requires outputs from all configured HCP workspaces, with no extra workspaces."
        )
    groups = {name: {"hosts": {}} for name in ROLE_GROUPS.values()}
    children = {"k3s_cluster": {"children": groups}}
    accounts = set()
    hostnames = set()
    public_addresses = set()

    for workspace in WORKSPACES:
        output = outputs[workspace]
        if output.get("commit") != commit:
            raise DeploymentError(
                f"{workspace}: outputs do not match deployment commit {commit}."
            )
        account = output.get("account")
        if not isinstance(account, str) or not account:
            raise DeploymentError(
                f"{workspace}: inventory output needs an account name."
            )
        account_group = account.replace("-", "_")
        if (
            not account_group.isascii()
            or not account_group.isidentifier()
            or account_group in children
            or account_group in groups
            or account_group in {"all", "ungrouped"}
        ):
            raise DeploymentError(
                f"{workspace}: account name cannot be used as a unique Ansible group."
            )
        if account in accounts:
            raise DeploymentError(f"{workspace}: duplicate account {account}.")
        accounts.add(account)
        hosts = output.get("hosts")
        if not isinstance(hosts, dict):
            raise DeploymentError(
                f"{workspace}: inventory hosts must be a map keyed by hostname."
            )
        children[account_group] = {"hosts": {}}

        for hostname, node in sorted(hosts.items()):
            if (
                not isinstance(hostname, str)
                or not hostname.startswith(account + "-")
                or hostname in hostnames
            ):
                raise DeploymentError(
                    f"{workspace}: hostname {hostname!r} must be unique and prefixed with its account."
                )
            if not isinstance(node, dict) or node.get("role") not in ROLE_GROUPS:
                raise DeploymentError(
                    f"{workspace}: {hostname} has an unsupported node role."
                )
            addresses = {}
            for field in ("public_ip", "private_ip"):
                try:
                    if not isinstance(node.get(field), str):
                        raise TypeError()
                    address = ipaddress.ip_address(node.get(field))
                    if address.version != 4:
                        raise ValueError()
                except (TypeError, ValueError):
                    raise DeploymentError(
                        f"{workspace}: {hostname} needs a valid {field} IPv4 address."
                    ) from None
                addresses[field] = str(address)
            if addresses["public_ip"] in public_addresses:
                raise DeploymentError(
                    f"{workspace}: multiple hosts have public IP {addresses['public_ip']}."
                )
            public_addresses.add(addresses["public_ip"])
            hostnames.add(hostname)
            groups[ROLE_GROUPS[node["role"]]]["hosts"][hostname] = {
                "ansible_host": addresses["public_ip"],
                "private_ip": addresses["private_ip"],
                "account": account,
                "node_role": node["role"],
                "hcp_workspace": workspace,
                "block_volumes": validate_volumes(
                    node.get("block_volumes"), f"{workspace}: {hostname}"
                ),
            }
            children[account_group]["hosts"][hostname] = {}

    if not hostnames:
        raise DeploymentError(
            "The configured workspaces returned no hosts; refusing to publish an empty inventory."
        )
    return {"all": {"vars": {"deployment_commit": commit}, "children": children}}


def write_inventory(inventory, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # JSON is accepted by Ansible's YAML inventory plugin, without a YAML dependency here.
    with open(
        path, "w", opener=lambda name, flags: os.open(name, flags, 0o600)
    ) as output:
        json.dump(inventory, output, indent=2, sort_keys=True)
        output.write("\n")
    path.chmod(0o600)


def build_applied_inventory(client, commit, repository=None):
    """Reuse fresh applied outputs only for unchanged, committed Terraform inputs."""
    preflight(client)
    outputs = fetch_outputs(client)
    commits = [output.get("commit") for output in outputs.values()]
    if (
        not commits
        or any(
            not isinstance(value, str)
            or len(value) != 40
            or any(char not in string.hexdigits for char in value)
            for value in commits
        )
        or len(set(commits)) != 1
    ):
        raise DeploymentError(
            "HCP outputs must share one applied commit; run a full deployment."
        )
    applied = commits[0]
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", applied, commit],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        paths = changed_paths(applied, commit, repository)
    except subprocess.CalledProcessError:
        raise DeploymentError(
            "Cannot verify the applied commit against this checkout; run a full deployment."
        ) from None
    if terraform_changed(paths):
        raise DeploymentError(
            "Terraform inputs differ from the applied HCP outputs; run a full deployment."
        )
    return build_inventory(outputs, applied)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        required=True,
        help="Expected deployed commit, or the host configuration commit with --reuse-applied.",
    )
    parser.add_argument(
        "--output", type=Path, default=Path(".generated/ansible/inventory.json")
    )
    parser.add_argument(
        "--reuse-applied",
        action="store_true",
        help="For host-only changes, verify unchanged Terraform against fresh applied outputs.",
    )
    args = parser.parse_args()
    client = HCP(os.environ.get("HCP_TERRAFORM_TOKEN"))
    inventory = (
        build_applied_inventory(client, args.commit)
        if args.reuse_applied
        else build_inventory(fetch_outputs(client), args.commit)
    )
    inventory["all"]["vars"]["configuration_commit"] = args.commit
    write_inventory(inventory, args.output)
    count = sum(
        len(group["hosts"])
        for group in inventory["all"]["children"]["k3s_cluster"]["children"].values()
    )
    print(
        f"Built inventory for {count} hosts from {len(WORKSPACES)} workspaces: {args.output}"
    )


if __name__ == "__main__":
    try:
        main()
    except (DeploymentError, OSError, ValueError) as error:
        print(f"Inventory generation failed: {error}", file=sys.stderr)
        sys.exit(1)
