import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_inventory, deployment_scope
from scripts.hcp_deploy import WORKSPACES, DeploymentError
from scripts.tests.test_inventory import fixture_outputs


class DeploymentScopeTests(unittest.TestCase):
    def test_changes_select_only_required_stages(self):
        for paths, expected in (
            (["terraform/live/main.tf"], (True, True, True)),
            (
                ["terraform/modules/oci-compute/cloud-init.yaml.tftpl"],
                (True, True, True),
            ),
            (["scripts/hcp_deploy.py"], (True, True, True)),
            ([".github/workflows/deployment.yml"], (True, True, True)),
            (["ansible/vars/k3s.yml"], (False, True, False)),
            (["ansible/roles/node/tasks/main.yml"], (False, True, False)),
            (["ansible/roles/k3s/templates/image-gc.conf.j2"], (False, True, False)),
            (["scripts/build_inventory.py"], (False, True, False)),
            (["ansible/roles/argocd/tasks/main.yml"], (False, True, True)),
            (["ansible/roles/infisical/tasks/main.yml"], (False, True, True)),
            (["ansible/playbooks/site.yml"], (False, True, True)),
            (["kubernetes/bootstrap/root.yaml"], (False, True, True)),
            (["kubernetes/bootstrap/applications/argocd.yaml"], (False, False, False)),
            (["kubernetes/workloads/example/values.yaml"], (False, False, False)),
            (["kubernetes/platform/argocd/config.yaml"], (False, False, False)),
            (["kubernetes/platform/longhorn/values.yaml"], (False, False, False)),
            (
                ["scripts/check_gitops.py", "scripts/tests/test_inventory.py"],
                (False, False, False),
            ),
            (
                ["docs/k3s.md", "ansible/README.md", "terraform/README.md"],
                (False, False, False),
            ),
            (["ansible/vars/k3s.yml", "terraform/live/outputs.tf"], (True, True, True)),
        ):
            with self.subTest(paths=paths):
                self.assertEqual(
                    tuple(deployment_scope.select_stages(paths).values()), expected
                )

    def test_manual_modes_and_unavailable_push_base_keep_safe_defaults(self):
        for mode, before, expected in (
            (
                "check",
                "a" * 40,
                {"terraform": True, "ansible": False, "bootstrap": False},
            ),
            (
                "deploy",
                "a" * 40,
                dict.fromkeys(("terraform", "ansible", "bootstrap"), True),
            ),
            (
                "auto",
                "0" * 40,
                dict.fromkeys(("terraform", "ansible", "bootstrap"), True),
            ),
            (
                "auto",
                "missing",
                dict.fromkeys(("terraform", "ansible", "bootstrap"), True),
            ),
        ):
            with (
                self.subTest(mode=mode, before=before),
                tempfile.TemporaryDirectory() as directory,
                patch.object(
                    sys,
                    "argv",
                    ["scope", "--commit", "b" * 40, "--before", before, "--mode", mode],
                ),
                patch.object(
                    deployment_scope,
                    "changed_paths",
                    side_effect=subprocess.CalledProcessError(1, "git"),
                ),
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_OUTPUT": str(Path(directory) / "outputs"),
                        "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary"),
                    },
                ),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                deployment_scope.main()
                self.assertEqual(json.loads(stdout.getvalue()), expected)
                self.assertEqual(
                    (Path(directory) / "outputs").read_text(),
                    "".join(
                        f"{key}={str(value).lower()}\n"
                        for key, value in expected.items()
                    ),
                )


class AppliedInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        self.write("terraform/live/main.tf", "# infrastructure\n")
        self.applied = self.commit()
        self.outputs = fixture_outputs()
        for output in self.outputs.values():
            output["commit"] = self.applied
        self.preflight = patch.object(build_inventory, "preflight").start()
        self.fetch = patch.object(
            build_inventory, "fetch_outputs", return_value=self.outputs
        ).start()
        self.addCleanup(patch.stopall)

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self):
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Tests",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        )
        return self.git("rev-parse", "HEAD")

    def test_host_changes_reuse_fresh_outputs_with_their_original_commit(self):
        self.write("ansible/vars/k3s.yml", "image_cleanup: true\n")
        self.write("terraform/README.md", "Documentation only.\n")
        target = self.commit()
        inventory = build_inventory.build_applied_inventory(object(), target, self.root)
        self.assertEqual(inventory["all"]["vars"]["deployment_commit"], self.applied)
        self.preflight.assert_called_once()
        self.fetch.assert_called_once()

    def test_unapplied_terraform_changes_cannot_use_old_inventory(self):
        self.write("terraform/live/main.tf", "# changed infrastructure\n")
        target = self.commit()
        with self.assertRaisesRegex(DeploymentError, "Terraform inputs differ"):
            build_inventory.build_applied_inventory(object(), target, self.root)

    def test_mixed_missing_or_invalid_workspace_outputs_are_rejected(self):
        for mutation in (
            lambda outputs: outputs[WORKSPACES[-1]].update(commit="b" * 40),
            lambda outputs: outputs[WORKSPACES[-1]].update(commit=None),
            lambda outputs: outputs.pop(WORKSPACES[-1]),
        ):
            outputs = copy.deepcopy(self.outputs)
            mutation(outputs)
            self.fetch.return_value = outputs
            with self.subTest(outputs=outputs), self.assertRaises(DeploymentError):
                build_inventory.build_applied_inventory(
                    object(), self.applied, self.root
                )

    def test_busy_hcp_preflight_prevents_inventory_reads(self):
        self.preflight.side_effect = DeploymentError("run is still applying")
        with self.assertRaisesRegex(DeploymentError, "still applying"):
            build_inventory.build_applied_inventory(object(), self.applied, self.root)
        self.fetch.assert_not_called()

    def test_unknown_applied_commit_cannot_be_used(self):
        for output in self.outputs.values():
            output["commit"] = "c" * 40
        with self.assertRaisesRegex(DeploymentError, "Cannot verify"):
            build_inventory.build_applied_inventory(object(), self.applied, self.root)

    def test_cli_records_host_commit_without_relabelling_applied_outputs(self):
        self.write("ansible/vars/k3s.yml", "image_cleanup: true\n")
        target = self.commit()
        result = build_inventory.build_applied_inventory(object(), target, self.root)
        destination = self.root / "inventory.json"
        with (
            patch.object(
                sys,
                "argv",
                [
                    "inventory",
                    "--commit",
                    target,
                    "--reuse-applied",
                    "--output",
                    str(destination),
                ],
            ),
            patch.object(build_inventory, "HCP"),
            patch.object(
                build_inventory, "build_applied_inventory", return_value=result
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            build_inventory.main()
        self.assertEqual(
            json.loads(destination.read_text())["all"]["vars"],
            {
                "deployment_commit": self.applied,
                "configuration_commit": target,
            },
        )


if __name__ == "__main__":
    unittest.main()
