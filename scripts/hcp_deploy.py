"""Send one committed Terraform snapshot to HCP, applying the hub before its peers."""

import argparse
import json
import os
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = "https://app.terraform.io/api/v2"
ORGANIZATION = "NullMonkeys"
WORKSPACES = tuple(f"eu-frankfurt-1-account-{number}" for number in range(1, 5))
TERMINAL = {
    "applied",
    "planned_and_finished",
    "planned_and_saved",
    "errored",
    "canceled",
    "force_canceled",
    "discarded",
}
FAILED = {"errored", "canceled", "force_canceled", "discarded", "policy_soft_failed"}


class DeploymentError(RuntimeError):
    pass


class HCP:
    def __init__(self, token):
        if not token:
            raise DeploymentError(
                "Set the HCP_TERRAFORM_TOKEN repository secret before deploying."
            )
        self.token = token

    def request(self, method, path, payload=None):
        request = urllib.request.Request(
            API + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/vnd.api+json",
            },
            method=method,
        )

        for attempt in range(5):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    body = response.read()
                return json.loads(body) if body else {}
            except urllib.error.HTTPError as error:
                if (
                    method == "GET"
                    and error.code in {429, 500, 502, 503, 504}
                    and attempt < 4
                ):
                    time.sleep(2**attempt)
                    continue
                raise DeploymentError(
                    f"HCP {method} {path} returned HTTP {error.code}."
                ) from None
            except urllib.error.URLError:
                if method == "GET" and attempt < 4:
                    time.sleep(2**attempt)
                    continue
                raise DeploymentError(
                    f"HCP {method} {path} failed; check connectivity and HCP runs before retrying."
                ) from None

    def items(self, path):
        page = 1
        while page:
            separator = "&" if "?" in path else "?"
            response = self.request(
                "GET", f"{path}{separator}page%5Bsize%5D=100&page%5Bnumber%5D={page}"
            )
            yield from response["data"]
            page = response.get("meta", {}).get("pagination", {}).get("next-page")

    def upload(self, url, archive):
        if urllib.parse.urlparse(url).scheme != "https":
            raise DeploymentError("HCP returned a non-HTTPS configuration upload URL.")

        request = urllib.request.Request(
            url,
            data=archive.read_bytes(),
            headers={"Content-Type": "application/octet-stream"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(request, timeout=120):
                pass
        except (urllib.error.URLError, TimeoutError):
            raise DeploymentError(
                "Configuration upload failed. No deployment run was requested."
            ) from None


def preflight(client):
    workspaces = {}
    for name in WORKSPACES:
        workspace = client.request(
            "GET", f"/organizations/{ORGANIZATION}/workspaces/{name}"
        )["data"]
        attributes = workspace["attributes"]
        if attributes.get("vcs-repo"):
            raise DeploymentError(
                f"{name}: disconnect VCS before enabling API-driven deployments."
            )
        if attributes.get("working-directory") != "terraform/live":
            raise DeploymentError(f"{name}: working directory must be terraform/live.")
        if attributes.get("execution-mode") != "remote":
            raise DeploymentError(f"{name}: execution mode must be remote.")
        if attributes.get("locked"):
            raise DeploymentError(f"{name}: workspace is locked.")
        triggers = list(
            client.items(
                f"/workspaces/{workspace['id']}/run-triggers?filter%5Brun-trigger%5D%5Btype%5D=inbound"
            )
        )
        if triggers:
            raise DeploymentError(
                f"{name}: remove native run triggers to prevent duplicate runs."
            )

        for run in client.items(f"/workspaces/{workspace['id']}/runs"):
            run_attributes = run["attributes"]
            if (
                not run_attributes.get("plan-only")
                and run_attributes["status"] not in TERMINAL
            ):
                raise DeploymentError(
                    f"{name}: run {run['id']} is still {run_attributes['status']}; finish it before deploying."
                )
        workspaces[name] = workspace["id"]
        print(f"{name}: ready", flush=True)
    return workspaces


def build_archive(commit, destination):
    if len(commit) != 40 or any(
        character not in string.hexdigits for character in commit
    ):
        raise DeploymentError("--commit must be the full 40-character Git commit SHA.")

    subprocess.run(
        [
            "git",
            "archive",
            "--format=tar.gz",
            f"--output={destination}",
            commit,
            "terraform",
        ],
        check=True,
    )


def wait_for_upload(client, configuration_id, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.request("GET", f"/configuration-versions/{configuration_id}")[
            "data"
        ]["attributes"]["status"]
        if status == "uploaded":
            return
        if status in {"errored", "archived"}:
            raise DeploymentError(f"Configuration {configuration_id} is {status}.")
        time.sleep(2)
    raise DeploymentError(
        f"Configuration {configuration_id} was not ready within {timeout} seconds."
    )


def wait_for_run(client, name, run_id, timeout=5400):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        attributes = client.request("GET", f"/runs/{run_id}")["data"]["attributes"]
        status = attributes["status"]
        if status != previous:
            print(f"{name}: {status}", flush=True)
            previous = status
        if status == "applied":
            return
        if (
            status == "planned_and_finished"
            and attributes.get("plan-only") is False
            and attributes.get("has-changes") is False
        ):
            return
        if status in FAILED or status in {"planned_and_finished", "planned_and_saved"}:
            raise DeploymentError(
                f"{name}: run {run_id} stopped at {status}; review it in HCP."
            )
        time.sleep(10)
    raise DeploymentError(
        f"{name}: run {run_id} timed out waiting for HCP. It may still be running; review it before retrying."
    )


def start_and_wait(client, name, workspace_id, configuration_id, commit):
    run = client.request(
        "POST",
        "/runs",
        {
            "data": {
                "type": "runs",
                "attributes": {
                    "message": f"GitHub NullMonkeys/infrastructure @ {commit}",
                    "auto-apply": True,
                    "allow-empty-apply": True,
                    "plan-only": False,
                    "variables": [
                        {"key": "deployment_commit", "value": json.dumps(commit)}
                    ],
                },
                "relationships": {
                    "workspace": {"data": {"type": "workspaces", "id": workspace_id}},
                    "configuration-version": {
                        "data": {
                            "type": "configuration-versions",
                            "id": configuration_id,
                        }
                    },
                },
            }
        },
    )["data"]
    url = f"https://app.terraform.io/app/{ORGANIZATION}/workspaces/{name}/runs/{run['id']}"
    print(f"{name}: {url}", flush=True)
    wait_for_run(client, name, run["id"])
    return url


def deploy(client, workspaces, commit, archive):
    hub, *peers = WORKSPACES
    configuration = client.request(
        "POST",
        f"/workspaces/{workspaces[hub]}/configuration-versions",
        {
            "data": {
                "type": "configuration-versions",
                "attributes": {"auto-queue-runs": False, "speculative": False},
            }
        },
    )["data"]
    client.upload(configuration["attributes"]["upload-url"], archive)
    wait_for_upload(client, configuration["id"])

    results = {
        hub: start_and_wait(client, hub, workspaces[hub], configuration["id"], commit)
    }
    failures = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                start_and_wait,
                client,
                name,
                workspaces[name],
                configuration["id"],
                commit,
            ): name
            for name in peers
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as error:  # noqa: BLE001
                failures.append(f"{name}: {error}")
    if failures:
        raise DeploymentError("Some peer runs failed:\n" + "\n".join(failures))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "deploy"])
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.mode == "deploy" and not args.commit:
        parser.error("deploy requires --commit")
    client = HCP(os.environ.get("HCP_TERRAFORM_TOKEN"))
    workspaces = preflight(client)
    if args.mode == "check":
        return
    with tempfile.TemporaryDirectory(prefix="hcp-terraform-") as directory:
        archive = Path(directory) / "configuration.tar.gz"
        build_archive(args.commit, archive)
        results = deploy(client, workspaces, args.commit, archive)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as output:
            output.write(f"Deployed commit `{args.commit}` through HCP Terraform.\n\n")
            output.writelines(f"- [{name}]({results[name]})\n" for name in WORKSPACES)


if __name__ == "__main__":
    try:
        main()
    except (DeploymentError, OSError, subprocess.CalledProcessError) as error:
        print(f"Deployment failed: {error}", file=sys.stderr)
        sys.exit(1)
