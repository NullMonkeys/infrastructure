import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ArgoBootstrapTests(unittest.TestCase):
    def test_existing_installation_is_not_reinstalled_without_explicit_recovery(self):
        inventory = os.environ.get("ANSIBLE_INVENTORY_BIN")
        if not inventory:
            self.skipTest(
                "Set ANSIBLE_INVENTORY_BIN to evaluate installation ownership."
            )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for server, recover, expected in (
                ("", False, True),
                ("{}", False, False),
                ("{}", True, True),
            ):
                with self.subTest(server=server, recover=recover):
                    playbook = directory / "decision.json"
                    playbook.write_text(
                        json.dumps(
                            [
                                {
                                    "hosts": "all",
                                    "connection": "local",
                                    "gather_facts": False,
                                    "vars": {
                                        "argocd_server": {"stdout": server},
                                        "argocd_recover": recover,
                                    },
                                    "tasks": [
                                        {
                                            "ansible.builtin.import_tasks": str(
                                                ROOT
                                                / "ansible/roles/argocd/tasks/main.yml"
                                            )
                                        },
                                        {
                                            "ansible.builtin.assert": {
                                                "that": [
                                                    f"argocd_seed_install == {'true' if expected else 'false'}"
                                                ]
                                            },
                                            "tags": ["argocd_install_decision"],
                                        },
                                    ],
                                }
                            ]
                        )
                    )
                    result = subprocess.run(
                        [
                            str(Path(inventory).with_name("ansible-playbook")),
                            "-i",
                            "localhost,",
                            "--tags",
                            "argocd_install_decision",
                            str(playbook),
                        ],
                        env=dict(
                            os.environ,
                            ANSIBLE_CONFIG=str(ROOT / "ansible/ansible.cfg"),
                            ANSIBLE_LOCAL_TEMP=str(directory / "tmp"),
                        ),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )

    def test_real_ansible_ownership_guard_without_repository_credentials(self):
        inventory = os.environ.get("ANSIBLE_INVENTORY_BIN")
        if not inventory:
            self.skipTest(
                "Set ANSIBLE_INVENTORY_BIN to evaluate the Argo bootstrap guards."
            )
        owned = {
            "metadata": {
                "labels": {"app.kubernetes.io/managed-by": "infrastructure-ansible"}
            }
        }
        cases = [
            ("fresh installation without credentials", "", True),
            ("managed installation without credentials", json.dumps(owned), True),
            (
                "unmanaged installation",
                json.dumps({"metadata": {}}),
                False,
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            playbook = directory / "guards.json"
            env = dict(
                os.environ,
                ANSIBLE_CONFIG=str(ROOT / "ansible/ansible.cfg"),
                ANSIBLE_LOCAL_TEMP=str(directory / "tmp"),
            )
            for label, namespace, success in cases:
                with self.subTest(case=label):
                    playbook.write_text(
                        json.dumps(
                            [
                                {
                                    "hosts": "all",
                                    "connection": "local",
                                    "gather_facts": False,
                                    "vars": {
                                        "argocd_namespace": {"stdout": namespace},
                                    },
                                    "tasks": [
                                        {
                                            "ansible.builtin.import_tasks": str(
                                                ROOT
                                                / "ansible/roles/argocd/tasks/main.yml"
                                            )
                                        }
                                    ],
                                }
                            ]
                        )
                    )
                    result = subprocess.run(
                        [
                            str(Path(inventory).with_name("ansible-playbook")),
                            "-i",
                            "localhost,",
                            "--tags",
                            "argocd_validation",
                            str(playbook),
                        ],
                        check=False,
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        result.returncode == 0, success, result.stdout + result.stderr
                    )


if __name__ == "__main__":
    unittest.main()
