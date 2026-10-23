import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import build_inventory as inventory
from scripts.hcp_deploy import WORKSPACES, DeploymentError

COMMIT = "a" * 40


def fixture_outputs():
    return {
        workspace: {
            "account": f"account-{index}",
            "commit": COMMIT,
            "hosts": {
                f"account-{index}-node-1": {
                    "role": "control-plane" if index == 1 else "worker",
                    "public_ip": f"203.0.113.{index}",
                    "private_ip": f"10.{index}.1.10",
                    "block_volumes": {},
                }
            },
        }
        for index, workspace in enumerate(WORKSPACES, 1)
    }


class FakeHCP:
    def __init__(self):
        self.outputs = fixture_outputs()
        self.sensitive = False
        self.missing = False

    def request(self, method, path):
        assert method == "GET"
        return {"data": {"id": path.rsplit("/", 1)[-1]}}

    def items(self, path):
        workspace = path.split("/")[2]
        assert path.endswith("/current-state-version-outputs")
        return iter(
            [
                {
                    "attributes": {
                        "name": "ignored_sensitive_output",
                        "sensitive": True,
                        "value": "do-not-copy",
                    }
                },
                {
                    "attributes": {
                        "name": "missing" if self.missing else "inventory",
                        "sensitive": self.sensitive,
                        "value": self.outputs[workspace],
                    }
                },
            ]
        )


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.outputs = fixture_outputs()

    def test_hosts_are_grouped_by_role_and_account(self):
        result = inventory.build_inventory(self.outputs, COMMIT)
        children = result["all"]["children"]
        roles = children["k3s_cluster"]["children"]
        self.assertEqual(len(roles["control_plane"]["hosts"]), 1)
        self.assertEqual(len(roles["workers"]["hosts"]), 3)
        for index in range(1, 5):
            self.assertIn(
                f"account-{index}-node-1", children[f"account_{index}"]["hosts"]
            )
        host = roles["control_plane"]["hosts"]["account-1-node-1"]
        self.assertEqual(host["ansible_host"], "203.0.113.1")
        self.assertEqual(host["private_ip"], "10.1.1.10")
        self.assertEqual(host["hcp_workspace"], WORKSPACES[0])
        self.assertEqual(result["all"]["vars"]["deployment_commit"], COMMIT)

    def test_fetch_uses_output_api_without_copying_other_outputs(self):
        result = inventory.fetch_outputs(FakeHCP())
        self.assertEqual(result, self.outputs)
        self.assertNotIn("do-not-copy", json.dumps(result))

    def test_missing_output_and_sensitive_inventory_are_errors(self):
        for setting in ("missing", "sensitive"):
            with self.subTest(setting=setting):
                client = FakeHCP()
                setattr(client, setting, True)
                with self.assertRaises(DeploymentError):
                    inventory.fetch_outputs(client)

    def test_a_missing_account_cannot_produce_partial_inventory(self):
        del self.outputs[WORKSPACES[-1]]
        with self.assertRaisesRegex(DeploymentError, "all configured"):
            inventory.build_inventory(self.outputs, COMMIT)

    def test_mixed_deployment_commits_are_rejected(self):
        self.outputs[WORKSPACES[-1]]["commit"] = "b" * 40
        with self.assertRaisesRegex(DeploymentError, "do not match deployment"):
            inventory.build_inventory(self.outputs, COMMIT)

    def test_duplicate_account_is_rejected(self):
        self.outputs[WORKSPACES[1]]["account"] = "account-1"
        with self.assertRaises(DeploymentError):
            inventory.build_inventory(self.outputs, COMMIT)

    def test_account_group_name_collision_is_rejected(self):
        self.outputs[WORKSPACES[1]]["account"] = "account_1"
        with self.assertRaisesRegex(DeploymentError, "unique Ansible group"):
            inventory.build_inventory(self.outputs, COMMIT)

    def test_hostnames_must_be_account_scoped(self):
        host = self.outputs[WORKSPACES[0]]["hosts"].pop("account-1-node-1")
        self.outputs[WORKSPACES[0]]["hosts"]["node-1"] = host
        with self.assertRaisesRegex(DeploymentError, "prefixed with its account"):
            inventory.build_inventory(self.outputs, COMMIT)

    def test_invalid_role_and_missing_addresses_are_rejected(self):
        for field, value in (
            ("role", "other"),
            ("public_ip", None),
            ("private_ip", "invalid"),
            ("public_ip", 1),
        ):
            with self.subTest(field=field, value=value):
                outputs = copy.deepcopy(self.outputs)
                outputs[WORKSPACES[0]]["hosts"]["account-1-node-1"][field] = value
                with self.assertRaises(DeploymentError):
                    inventory.build_inventory(outputs, COMMIT)

    def test_duplicate_ssh_targets_are_rejected(self):
        self.outputs[WORKSPACES[1]]["hosts"]["account-2-node-1"]["public_ip"] = (
            "203.0.113.1"
        )
        with self.assertRaisesRegex(DeploymentError, "multiple hosts"):
            inventory.build_inventory(self.outputs, COMMIT)

    def test_volume_attachment_details_reach_host_inventory(self):
        volumes = {
            "data": {
                "device": "/dev/oracleoci/oraclevdb",
                "attachment_type": "paravirtualized",
                "is_read_only": False,
            }
        }
        self.outputs[WORKSPACES[0]]["hosts"]["account-1-node-1"]["block_volumes"] = (
            volumes
        )
        result = inventory.build_inventory(self.outputs, COMMIT)
        host = result["all"]["children"]["k3s_cluster"]["children"]["control_plane"][
            "hosts"
        ]["account-1-node-1"]
        self.assertEqual(host["block_volumes"], volumes)

    def test_unsafe_or_missing_volume_metadata_is_rejected(self):
        valid = {
            "device": "/dev/oracleoci/oraclevdb",
            "attachment_type": "paravirtualized",
            "is_read_only": False,
        }
        for volumes in (
            None,
            {"data": dict(valid, device=None)},
            {"data": dict(valid, device="/dev/oracleoci/oraclevda")},
            {"data": dict(valid, device="/dev/sdb")},
            {"data": dict(valid, is_read_only=True)},
            {"data": dict(valid, attachment_type="iscsi")},
            {"first": valid, "second": valid},
        ):
            with self.subTest(volumes=volumes), self.assertRaises(DeploymentError):
                inventory.validate_volumes(volumes, "fixture")

    def test_an_empty_account_is_valid_but_an_empty_cluster_is_not(self):
        self.outputs[WORKSPACES[-1]]["hosts"] = {}
        inventory.build_inventory(self.outputs, COMMIT)
        for output in self.outputs.values():
            output["hosts"] = {}
        with self.assertRaisesRegex(DeploymentError, "no hosts"):
            inventory.build_inventory(self.outputs, COMMIT)

    def test_inventory_is_deterministic_and_written_with_private_permissions(self):
        expected = inventory.build_inventory(self.outputs, COMMIT)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ansible/inventory.json"
            inventory.write_inventory(expected, path)
            first = path.read_bytes()
            inventory.write_inventory(expected, path)
            self.assertEqual(first, path.read_bytes())
            self.assertEqual(json.loads(first), expected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_ansible_can_parse_generated_inventory_without_connecting_to_hosts(self):
        command = os.environ.get("ANSIBLE_INVENTORY_BIN")
        if not command:
            self.skipTest("Set ANSIBLE_INVENTORY_BIN to run the real Ansible parser.")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".generated/ansible/inventory.json"
            inventory.write_inventory(
                inventory.build_inventory(self.outputs, COMMIT), path
            )
            config = root / "ansible/ansible.cfg"
            config.parent.mkdir()
            config.write_text(
                (
                    Path(__file__).resolve().parents[2] / "ansible/ansible.cfg"
                ).read_text()
            )
            env = dict(
                os.environ,
                ANSIBLE_CONFIG=str(config),
                ANSIBLE_LOCAL_TEMP=str(root / "tmp"),
            )
            result = subprocess.run(
                [command, "--list"], env=env, check=True, capture_output=True, text=True
            )
            parsed = json.loads(result.stdout)
            self.assertEqual(
                set(parsed["_meta"]["hostvars"]),
                {f"account-{index}-node-1" for index in range(1, 5)},
            )
            self.assertEqual(
                parsed["_meta"]["hostvars"]["account-1-node-1"]["ansible_host"],
                "203.0.113.1",
            )
            self.assertEqual(parsed["account_4"]["hosts"], ["account-4-node-1"])
            self.assertEqual(len(parsed["workers"]["hosts"]), 3)

            repository = Path(__file__).resolve().parents[2]
            playbook = repository / "ansible/playbooks/site.yml"
            env["ANSIBLE_ROLES_PATH"] = str(repository / "ansible/roles")
            env["ANSIBLE_FILTER_PLUGINS"] = str(repository / "ansible/filter_plugins")
            playbook_command = str(Path(command).with_name("ansible-playbook"))
            subprocess.run(
                [playbook_command, "--syntax-check", str(playbook)],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            targets = subprocess.run(
                [playbook_command, "--list-hosts", str(playbook)],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            for index in range(1, len(WORKSPACES) + 1):
                self.assertIn(f"account-{index}-node-1", targets.stdout)


if __name__ == "__main__":
    unittest.main()
