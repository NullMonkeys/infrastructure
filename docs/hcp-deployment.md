# HCP Terraform deployment

For repository instructions, see [AGENTS.md](../AGENTS.md). For checks that do not
contact HCP or cluster hosts, see [development](development.md).

Each OCI tenancy has its own HCP workspace, variables, credentials, and state.
GitHub Actions supplies committed code through the HCP API. HCP executes every plan
and apply.

## Flow

PRs targeting `main` run the separate Terraform, Ansible, and Kubernetes checks workflows.
Terraform checks include formatting, lint, validation, and offline tests. Ansible
checks test inventory generation with mocked HCP outputs and Ansible's actual parser.
They also validate deployment playbook syntax, storage-safety decisions, and K3s
configuration rendering offline.
Kubernetes checks render Kustomize roots and pinned Helm charts, then validate
native/custom resources and CRDs against release-matched schemas.

A push to `main` selects stages from its complete before/after commit diff, without
repeating PR checks:

| Change                                                                              | Automatic work                                                            |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Terraform inputs, deployment orchestration scripts, or deployment workflow          | Full HCP → inventory → hosts/K3s → GitOps bootstrap                       |
| Node/K3s roles, K3s variables, host playbooks, host filters, or inventory generator | Fresh verified inventory → hosts/K3s; skip HCP apply and GitOps bootstrap |
| Argo/Infisical roles, other Ansible configuration, or top-level bootstrap YAML      | Fresh verified inventory → hosts/K3s → GitOps bootstrap; skip HCP apply   |
| Service manifests or nested Application definitions                                 | Argo reconciliation; no Deployment workflow                               |
| Markdown, script tests, or GitOps validation script only                            | No Deployment workflow                                                    |

The full deployment retains this ordering:

1. Actions checks configured workspaces are API-driven, idle, and have no native run triggers.
2. `git archive` packages the exact commit's `terraform/` directory, retaining
   `terraform/live` and the sibling modules. Uncommitted files and provider caches
   are excluded.
3. Actions uploads one configuration version without automatically queueing a run.
4. The hub workspace plans and applies that version. Empty applies are allowed.
5. After the hub succeeds, peer workspaces run in parallel using the same configuration
   version and their own workspace variables. All peer runs must finish successfully.
6. The `inventory` job reads each workspace's HCP inventory output, verifies its commit
   matches this deployment, and writes `.generated/ansible/inventory.json`.
7. Actions displays its groups and saves the inventory as an artifact for seven days.
8. The `ansible` job downloads that inventory and prepares IPv4-only hosts, packages,
   and storage, rebooting hosts one at a time when required to disable IPv6.
9. The same job bootstraps K3s, joins remaining servers and workers, and checks node
   readiness, private etcd metrics, DNS, service routing, and cross-node pod traffic.
10. Ansible supplies the Infisical machine credential, seeds Argo CD if absent, and
    configures repository access and bootstrap
    projects/root, and waits for Argo's own controllers. Child Applications reconcile
    asynchronously without blocking Actions. Argo manages its own installation,
    ingress, and certificates; normal Ansible runs do not overwrite that configuration. See [GitOps](gitops.md).

Push and manual deployments share a concurrency group with cancellation disabled
and `queue: max`. Up to 100 pending workflow runs can queue; they do not replace each
other. The workflow-wide concurrency group is `infrastructure-main` and covers host
preparation, K3s verification, and Argo bootstrap as well. PR checks cancel superseded checks and do not receive the
deployment token.

## Inventory generation

When selected, the `terraform` job waits for every workspace, rather than just submitting HCP runs.
A peer failure fails the job; a slow peer keeps it running. The dependent `inventory`
job runs only after all configured workspaces succeed. For host-only changes,
Terraform is deliberately skipped and inventory instead uses `--reuse-applied`.
Both inventory and Ansible are skipped for manual `check` mode. The workflow's
concurrency lock remains held through host preparation.

Host-only inventory performs the same idle-workspace preflight, fetches fresh
outputs from every workspace, requires one common applied commit, and verifies
that it is an ancestor of the configuration commit with identical Terraform
inputs (Markdown is excluded). A missing commit, changed Terraform input, partial
apply, missing workspace, or active HCP run stops configuration. No previous
artifact or cached inventory is used as a fallback. The checkout includes full
history for these checks.

`deployment_commit` in inventory remains the actual Terraform applied commit;
`configuration_commit` records the current host configuration commit. Full
deployments set both to the same SHA. A prior failed infrastructure deployment
can make a subsequent host-only run fail this guard; finish the HCP runs and use
manual `deploy` to apply the complete current commit before retrying.

The `ansible` job depends on `inventory` and runs `ansible/playbooks/site.yml`, including
host preparation and K3s. `gitops_bootstrap_enabled` controls the final Argo and
Infisical bootstrap import; ordinary node/K3s changes skip that import. Bootstrap
does not wait for child workloads or require root-Application health. Direct local
site runs default to the complete sequence.
The job is skipped in manual `check` mode. See
[host preparation](host-preparation.md) for prerequisites and storage behavior, and
the [Ansible setup](../ansible/README.md) for inventory details. The [K3s guide](k3s.md)
describes configuration, networking checks, and partial-installation recovery.

## Required workspace and repository configuration

- Repository secret `HCP_TERRAFORM_TOKEN` in `NullMonkeys/infrastructure`.
  Use a user/team API token with permission to read workspaces and runs, upload
  configurations, queue plans, and apply in every configured workspace. Organization tokens
  cannot perform this workflow. The token also needs permission to read workspace
  outputs for inventory generation. OCI credentials stay in the HCP workspaces.
- All configured workspaces use API-driven workflow, remote execution, and working
  directory `terraform/live`. Direct VCS connections must remain disconnected.
- No native workspace run triggers. Actions explicitly queues and waits for the
  peer runs; native triggers would create duplicate runs.
- Repository secret `ANSIBLE_SSH_PRIVATE_KEY` matching the nodes' Terraform public
  key, and optional repository variable `ANSIBLE_REMOTE_USER` (default `ubuntu`).
  The remote user needs passwordless sudo. Volume devices must satisfy the
  [host preparation prerequisites](host-preparation.md).
- Repository secrets `INFISICAL_CLIENT_ID` and `INFISICAL_CLIENT_SECRET` for the
  [Infisical bootstrap](secrets.md).
- Public repository `https://github.com/NullMonkeys/infrastructure.git`. Argo
  reads it anonymously over HTTPS; no Argo repository key or token is needed.

These are the expected settings for the implemented pipeline, not a cutover to
repeat on each change. The deployment preflight verifies workspace settings live.
To check setup without deploying, run the Deployment workflow manually with mode
`check`. It still needs the HCP token. Manual execution is restricted to `main`.
The push paths are `terraform/**`, `ansible/**`, `scripts/**`,
`kubernetes/bootstrap/*.yaml`, and `.github/workflows/deployment.yml`, excluding
Markdown within Terraform/Ansible, `scripts/tests/**`, and `scripts/check_gitops.py`.
Documentation-only and Kubernetes workload-only pushes do not trigger this pipeline.
Argo independently reconciles its configured paths on `main`; it does not wait for
Actions. Merge prerequisite infrastructure changes first when ordering matters.
Manual `deploy` always runs the full sequence, including GitOps bootstrap. Use it
for recovery, credential rotation, or changes made directly to HCP workspace
variables; Git path filters cannot detect those external changes. A new branch or
an unavailable force-push base also selects a full deployment conservatively.

`auto-apply` and `allow-empty-apply` are requested explicitly on each API run. The
same commit is also passed as the run-specific `deployment_commit` variable so
the inventory outputs can be checked against the expected release.
Workspace-wide auto-apply settings do not need changing. Policy checks remain in effect;
the script does not override policies or force-unlock workspaces.

## Failures and retries

A hub failure stops the pipeline before any peer run is created. A peer failure
fails the workflow after the other peers finish; successful applies are not rolled back.
Run links appear in Actions logs and, on success, its summary.

If Actions is canceled or times out, HCP may still be executing a run. The next release
refuses to start while any workspace has an unfinished non-speculative run. Review
and finish/discard that run in HCP, then rerun the failed workflow. Reruns deploy the
same commit again; Terraform plans the remaining changes.

Run orchestration tests locally with:

```sh
python3 -m unittest discover -s scripts/tests -v
```

References: [HCP API workflow](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/run/api),
[runs API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run),
[configuration versions](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/configuration-versions),
[GitHub concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

Infisical bootstrap additionally needs repository secrets `INFISICAL_CLIENT_ID` and
`INFISICAL_CLIENT_SECRET`. The Cloudflare token is fetched by the in-cluster operator,
not Actions. See [Infisical setup](secrets.md) for the source path and rotation flow.
