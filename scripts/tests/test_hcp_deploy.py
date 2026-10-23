import contextlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from unittest.mock import patch

from scripts import hcp_deploy as deploy

COMMIT = "a" * 40


class FakeHCP:
    def __init__(self):
        self.ids = {
            name: f"ws-{index}" for index, name in enumerate(deploy.WORKSPACES, 1)
        }
        self.workspaces = {
            name: {
                "id": workspace_id,
                "attributes": {
                    "vcs-repo": None,
                    "working-directory": "terraform/live",
                    "execution-mode": "remote",
                    "locked": False,
                },
            }
            for name, workspace_id in self.ids.items()
        }
        self.existing_runs = {}
        self.triggers = {}
        self.created = []
        self.completed = []
        self.fail_workspace = None
        self.status = "applied"
        self.plan_only = False
        self.has_changes = False
        self.uploaded = False

    def request(self, method, path, payload=None):
        if method == "GET" and "/organizations/" in path:
            return {"data": self.workspaces[path.rsplit("/", 1)[1]]}
        if method == "POST" and path.endswith("/configuration-versions"):
            assert payload["data"]["attributes"] == {  # ty: ignore[not-subscriptable]
                "auto-queue-runs": False,
                "speculative": False,
            }
            return {
                "data": {
                    "id": "cv-exact",
                    "attributes": {"upload-url": "https://upload.example/config"},
                }
            }
        if method == "GET" and path.startswith("/configuration-versions/"):
            return {
                "data": {
                    "attributes": {"status": "uploaded" if self.uploaded else "pending"}
                }
            }
        if method == "POST" and path == "/runs":
            assert self.uploaded
            workspace_id = payload["data"]["relationships"]["workspace"]["data"]["id"]  # ty: ignore[not-subscriptable]
            if workspace_id != "ws-1":
                assert "ws-1" in self.completed, (
                    "A peer was started before the hub completed."
                )
            self.created.append(payload)
            return {"data": {"id": f"run-{workspace_id}"}}
        if method == "GET" and path.startswith("/runs/"):
            workspace_id = path.removeprefix("/runs/run-")
            if workspace_id == self.fail_workspace:
                status = "errored"
            else:
                status = self.status
                self.completed.append(workspace_id)
            return {
                "data": {
                    "attributes": {
                        "status": status,
                        "plan-only": self.plan_only,
                        "has-changes": self.has_changes,
                    }
                }
            }
        raise AssertionError((method, path))

    def items(self, path):
        workspace_id = path.split("/")[2]
        if "/run-triggers" in path:
            return iter(self.triggers.get(workspace_id, []))
        return iter(self.existing_runs.get(workspace_id, []))

    def upload(self, url, archive):
        assert url == "https://upload.example/config"
        assert isinstance(archive, Path)
        self.uploaded = True


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeHCP()
        self.logs = contextlib.redirect_stdout(io.StringIO())
        self.logs.__enter__()
        self.addCleanup(self.logs.__exit__, None, None, None)

    def test_hub_finishes_before_peers_and_every_run_uses_same_snapshot(self):
        workspaces = deploy.preflight(self.client)
        results = deploy.deploy(self.client, workspaces, COMMIT, Path("unused.tar.gz"))
        self.assertEqual(set(results), set(deploy.WORKSPACES))
        self.assertEqual(len(self.client.created), 4)
        for request in self.client.created:
            data = request["data"]
            self.assertEqual(
                data["relationships"]["configuration-version"]["data"]["id"], "cv-exact"
            )
            self.assertTrue(data["attributes"]["allow-empty-apply"])
            self.assertTrue(data["attributes"]["auto-apply"])
            self.assertFalse(data["attributes"]["plan-only"])
            self.assertIn(COMMIT, data["attributes"]["message"])
            self.assertEqual(
                data["attributes"]["variables"],
                [{"key": "deployment_commit", "value": json.dumps(COMMIT)}],
            )

    def test_hub_failure_does_not_start_peers(self):
        self.client.fail_workspace = "ws-1"
        with self.assertRaisesRegex(deploy.DeploymentError, "errored"):
            deploy.deploy(self.client, self.client.ids, COMMIT, Path("unused.tar.gz"))
        self.assertEqual(len(self.client.created), 1)

    def test_peer_failure_fails_deployment_after_waiting_for_all_peers(self):
        self.client.fail_workspace = "ws-2"
        with self.assertRaisesRegex(deploy.DeploymentError, "Some peer runs failed"):
            deploy.deploy(self.client, self.client.ids, COMMIT, Path("unused.tar.gz"))
        self.assertEqual(len(self.client.created), 4)
        self.assertIn("ws-3", self.client.completed)
        self.assertIn("ws-4", self.client.completed)

    def test_deployment_cannot_finish_while_one_account_is_still_applying(self):
        slow_peer_started = threading.Event()
        release_slow_peer = threading.Event()

        def finish_run(client, name, workspace_id, configuration_id, commit):
            if name == deploy.WORKSPACES[-1]:
                slow_peer_started.set()
                if not release_slow_peer.wait(timeout=5):
                    raise AssertionError("Test did not release the last account.")
            return f"https://app.terraform.io/runs/{workspace_id}"

        with (
            patch.object(deploy, "start_and_wait", side_effect=finish_run),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            deployment = executor.submit(
                deploy.deploy,
                self.client,
                self.client.ids,
                COMMIT,
                Path("unused.tar.gz"),
            )
            try:
                self.assertTrue(slow_peer_started.wait(timeout=5))
                with self.assertRaises(FutureTimeoutError):
                    deployment.result(timeout=0.1)
            finally:
                release_slow_peer.set()
            self.assertEqual(set(deployment.result(timeout=5)), set(deploy.WORKSPACES))

    def test_no_change_standard_run_can_continue(self):
        self.client.status = "planned_and_finished"
        deploy.wait_for_run(self.client, deploy.WORKSPACES[0], "run-ws-1")

    def test_speculative_run_cannot_count_as_success(self):
        self.client.status = "planned_and_finished"
        self.client.plan_only = True
        with self.assertRaises(deploy.DeploymentError):
            deploy.wait_for_run(self.client, deploy.WORKSPACES[0], "run-ws-1")

    def test_policy_failure_is_not_overridden(self):
        self.client.status = "policy_soft_failed"
        with self.assertRaisesRegex(deploy.DeploymentError, "policy_soft_failed"):
            deploy.wait_for_run(self.client, deploy.WORKSPACES[0], "run-ws-1")
        self.assertFalse(self.client.created)

    def test_connected_vcs_stops_before_upload_or_run(self):
        self.client.workspaces[deploy.WORKSPACES[0]]["attributes"]["vcs-repo"] = {  # ty: ignore[invalid-assignment]
            "identifier": "NullMonkeys/infrastructure"
        }
        with self.assertRaisesRegex(deploy.DeploymentError, "disconnect VCS"):
            deploy.preflight(self.client)
        self.assertFalse(self.client.uploaded)
        self.assertFalse(self.client.created)

    def test_native_triggers_stop_before_run(self):
        self.client.triggers["ws-2"] = [{"id": "rt-old"}]
        with self.assertRaisesRegex(deploy.DeploymentError, "native run triggers"):
            deploy.preflight(self.client)
        self.assertFalse(self.client.created)

    def test_old_running_release_blocks_new_release(self):
        self.client.existing_runs["ws-4"] = [
            {"id": "run-old", "attributes": {"status": "applying", "plan-only": False}}
        ]
        with self.assertRaisesRegex(deploy.DeploymentError, "still applying"):
            deploy.preflight(self.client)
        self.assertFalse(self.client.created)

    def test_stale_speculative_plan_does_not_block_deployment(self):
        self.client.existing_runs["ws-4"] = [
            {
                "id": "run-speculative",
                "attributes": {"status": "planning", "plan-only": True},
            }
        ]
        self.assertEqual(len(deploy.preflight(self.client)), 4)

    def test_timeout_does_not_claim_remote_run_was_canceled(self):
        self.client.status = "applying"
        with (
            patch.object(deploy.time, "monotonic", side_effect=[0, 0, 2]),
            patch.object(deploy.time, "sleep"),
            self.assertRaisesRegex(deploy.DeploymentError, "may still be running"),
        ):
            deploy.wait_for_run(
                self.client, deploy.WORKSPACES[0], "run-ws-1", timeout=1
            )

    def test_missing_token_is_a_clear_error(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "HCP_TERRAFORM_TOKEN"):
            deploy.HCP(None)


class TransportTests(unittest.TestCase):
    def test_upload_does_not_forward_api_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "config.tar.gz"
            archive.write_bytes(b"archive")
            with patch.object(deploy.urllib.request, "urlopen") as urlopen:
                deploy.HCP("test-api-token").upload(
                    "https://upload.example/config", archive
                )
            request = urlopen.call_args.args[0]
            self.assertFalse(request.has_header("Authorization"))
            self.assertEqual(request.data, b"archive")

    def test_create_run_is_not_retried_after_ambiguous_failure(self):
        with (
            patch.object(
                deploy.urllib.request,
                "urlopen",
                side_effect=deploy.urllib.error.URLError("lost response"),
            ) as urlopen,
            self.assertRaises(deploy.DeploymentError),
        ):
            deploy.HCP("test-api-token").request("POST", "/runs", {"data": {}})
        self.assertEqual(urlopen.call_count, 1)

    def test_rate_limited_reads_are_retried(self):
        error = deploy.urllib.error.HTTPError(
            "https://app.terraform.io/api/v2/runs/run-1", 429, "limited", {}, None
        )
        with (
            patch.object(deploy.urllib.request, "urlopen") as urlopen,
            patch.object(deploy.time, "sleep"),
        ):
            urlopen.side_effect = [
                error,
                contextlib.closing(
                    io.BytesIO(json.dumps({"data": {"id": "run-1"}}).encode())
                ),
            ]
            result = deploy.HCP("test-api-token").request("GET", "/runs/run-1")
        self.assertEqual(result["data"]["id"], "run-1")
        self.assertEqual(urlopen.call_count, 2)

    def test_pagination_checks_later_pages_for_runs(self):
        with patch.object(
            deploy.HCP,
            "request",
            side_effect=[
                {"data": [{"id": "first"}], "meta": {"pagination": {"next-page": 2}}},
                {"data": [{"id": "last"}], "meta": {"pagination": {"next-page": None}}},
            ],
        ) as request:
            self.assertEqual(
                [item["id"] for item in deploy.HCP("token").items("/runs")],
                ["first", "last"],
            )
        self.assertIn("page%5Bnumber%5D=2", request.call_args.args[1])


class ArchiveTests(unittest.TestCase):
    def test_archive_contains_exact_commit_and_sibling_modules_without_local_files(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True)
            (root / "terraform/live").mkdir(parents=True)
            (root / "terraform/modules/network").mkdir(parents=True)
            (root / "terraform/live/main.tf").write_text("committed root")
            (root / "terraform/modules/network/main.tf").write_text("committed module")
            subprocess.run(["git", "-C", directory, "add", "terraform"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    directory,
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            commit = subprocess.check_output(
                ["git", "-C", directory, "rev-parse", "HEAD"], text=True
            ).strip()
            (root / "terraform/live/main.tf").write_text("uncommitted change")
            (root / "terraform/live/secret.tfvars").write_text("untracked secret")
            archive = root / "configuration.tar.gz"
            previous_directory = Path.cwd()
            try:
                os.chdir(directory)
                deploy.build_archive(commit, archive)
            finally:
                os.chdir(previous_directory)
            with tarfile.open(archive) as contents:
                self.assertEqual(
                    contents.extractfile("terraform/live/main.tf").read(),
                    b"committed root",
                )
                self.assertIn("terraform/modules/network/main.tf", contents.getnames())
                self.assertNotIn("terraform/live/secret.tfvars", contents.getnames())


if __name__ == "__main__":
    unittest.main()
