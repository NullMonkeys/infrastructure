"""Select deployment stages from the committed push diff, without cloud access."""

import argparse
import json
import os
import subprocess
from pathlib import Path


def changed_paths(before, commit, repository=None):
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", before, commit, "--"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode().rstrip("\0").split("\0") if result.stdout else []


def terraform_changed(paths):
    return any(
        path.startswith("terraform/") and not path.endswith(".md") for path in paths
    )


def select_stages(paths):
    relevant = [
        path
        for path in paths
        if not path.endswith(".md")
        and not path.startswith("scripts/tests/")
        and path != "scripts/check_gitops.py"
    ]
    orchestration = any(
        path == ".github/workflows/deployment.yml"
        or path.startswith("scripts/")
        and path != "scripts/build_inventory.py"
        for path in relevant
    )
    terraform = terraform_changed(relevant) or orchestration
    bootstrap = terraform or any(
        (Path(path).parent == Path("kubernetes/bootstrap") and path.endswith(".yaml"))
        or (
            path.startswith("ansible/")
            and not path.startswith(
                (
                    "ansible/roles/node/",
                    "ansible/roles/k3s/",
                    "ansible/vars/k3s.yml",
                    "ansible/filter_plugins/",
                    "ansible/playbooks/prepare.yml",
                    "ansible/playbooks/k3s.yml",
                )
            )
        )
        for path in relevant
    )
    ansible = bootstrap or any(
        path.startswith("ansible/") or path == "scripts/build_inventory.py"
        for path in relevant
    )
    return {"terraform": terraform, "ansible": ansible, "bootstrap": bootstrap}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--before", default="")
    parser.add_argument("--mode", choices=("auto", "check", "deploy"), default="auto")
    args = parser.parse_args()
    if args.mode == "check":
        stages = {"terraform": True, "ansible": False, "bootstrap": False}
    elif args.mode == "deploy" or not args.before.strip("0"):
        stages = dict.fromkeys(("terraform", "ansible", "bootstrap"), True)
    else:
        try:
            stages = select_stages(changed_paths(args.before, args.commit))
        except subprocess.CalledProcessError:
            # A removed force-push base cannot safely prove which stages to skip.
            stages = dict.fromkeys(("terraform", "ansible", "bootstrap"), True)
    print(json.dumps(stages, sort_keys=True))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as stream:
            stream.writelines(
                f"{name}={str(enabled).lower()}\n" for name, enabled in stages.items()
            )
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as stream:
            stream.write(
                "Deployment stages: "
                + ", ".join(
                    f"{name}={'run' if enabled else 'skip'}"
                    for name, enabled in stages.items()
                )
                + "\n"
            )


if __name__ == "__main__":
    main()
