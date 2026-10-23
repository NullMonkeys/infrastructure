"""Exercise the actual bootstrap guards with synthetic data and no cluster access."""

import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class InfisicalBootstrapTests(unittest.TestCase):
    def test_credentials_can_be_seeded_reused_or_rotated_without_exposure(self):
        inventory = os.environ.get("ANSIBLE_INVENTORY_BIN")
        if not inventory:
            self.skipTest("Set ANSIBLE_INVENTORY_BIN to evaluate Infisical guards.")
        client_id = "synthetic-client-id"
        client_secret = "synthetic-client-secret"
        existing = json.dumps(
            {
                "data": {
                    "clientId": base64.b64encode(client_id.encode()).decode(),
                    "clientSecret": base64.b64encode(client_secret.encode()).decode(),
                }
            }
        )
        cases = (
            ("new identity", "", client_id, client_secret, True),
            ("reuse", existing, "", "", True),
            ("rotation", existing, client_id, client_secret, True),
            ("missing", "", "", "", False),
            ("partial rotation", existing, client_id, "", False),
            ("missing id", "", "", client_secret, False),
            (
                "empty stored credential",
                '{"data":{"clientId":"","clientSecret":""}}',
                "",
                "",
                False,
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for label, stored, supplied_id, supplied_secret, success in cases:
                with self.subTest(case=label):
                    playbook = directory / "guards.json"
                    playbook.write_text(
                        json.dumps(
                            [
                                {
                                    "hosts": "all",
                                    "connection": "local",
                                    "gather_facts": False,
                                    "vars": {
                                        "infisical_existing_credential": {
                                            "stdout": stored
                                        },
                                        "infisical_client_id": supplied_id,
                                        "infisical_client_secret": supplied_secret,
                                    },
                                    "tasks": [
                                        {
                                            "ansible.builtin.import_tasks": str(
                                                ROOT
                                                / "ansible/roles/infisical/tasks/main.yml"
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
                            "infisical_validation",
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
                    output = result.stdout + result.stderr
                    self.assertEqual(result.returncode == 0, success, output)
                    for sensitive in (
                        client_id,
                        client_secret,
                        base64.b64encode(client_secret.encode()).decode(),
                    ):
                        self.assertNotIn(sensitive, output)


if __name__ == "__main__":
    unittest.main()
