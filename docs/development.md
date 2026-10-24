# Development and local checks

Start with [AGENTS.md](../AGENTS.md) for architecture, contracts, and live-operation
boundaries. Run the commands below from the repository root. They do not apply
infrastructure or connect to cluster hosts. Initial dependency installation needs
network access; tests use mocked HCP responses and Terraform providers.

## Tools and versions

Use versions matching the repository rather than whichever binary is installed:

| Tool                   | Version source                                                                                      |
| ---------------------- | --------------------------------------------------------------------------------------------------- |
| Terraform              | `terraform/**/versions.tf` and `.github/workflows/terraform.yml`                                    |
| OCI and TFE providers  | `required_providers` and `terraform/live/.terraform.lock.hcl`                                       |
| TFLint                 | `tflint.required_version` in `.tflint.hcl`                                                         |
| Ansible                | `ansible/requirements.txt`                                                                          |
| Ansible collections    | `ansible/collections/requirements.yml`                                                              |
| K3s and installer hash | `ansible/vars/k3s.yml`                                                                              |
| Argo CD release        | `kubernetes/platform/argocd/kustomization.yaml`                                                     |
| Addon chart versions   | Each service’s `helmCharts` entry in `kubernetes/{platform,workloads}/<service>/kustomization.yaml` |
| Kubernetes validation  | `kubernetes/requirements.txt` and `.github/workflows/kubernetes.yml`                                |
| CI controller Python   | Setup steps in the Ansible and deployment workflows                                                 |

Check `terraform version` before using a system binary: an older installed version
may not satisfy the root and module constraints. Keep the root lock file; do not
commit generated locks under modules.

## Python and Ansible

Deployment/inventory scripts use the standard library. Run unit tests without credentials:

```sh
python3 -m unittest discover -s scripts/tests -v
```

That command skips the real Ansible integration test unless its executable is
explicitly supplied. For the full inventory parser check, install the pinned Ansible
release in an ignored virtual environment using Python 3.12 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r ansible/requirements.txt
.venv/bin/ansible-galaxy collection install -r ansible/collections/requirements.yml
ANSIBLE_INVENTORY_BIN="$PWD/.venv/bin/ansible-inventory" \
  .venv/bin/python -m unittest discover -s scripts/tests -v
```

The integration test creates a temporary inventory from synthetic workspace
outputs, parses it with Ansible, and checks `ansible/playbooks/site.yml` syntax and
targets. K3s tests render the actual server/worker configuration and temporary
networking workload, validate private addressing, and protect against accidental
second-cluster bootstrap. Storage tests exercise disk-safety decisions using synthetic probes.
IPv6 tests cover boot-option preservation, reboot decisions, and verification
without changing network settings or rebooting any host.
These checks need no HCP token, OCI credentials, SSH key, or provisioned hosts.
Argo tests exercise the actual Ansible namespace ownership and initial
installation/recovery guards without repository credentials, skipping every task
that could contact a cluster. Infisical tests similarly exercise credential-pair
validation, existing Secret reuse, and redaction with synthetic values.

## Kubernetes manifests

CI runs the official `ghcr.io/yannh/kubeconform:v0.8.0` image as a Docker action,
following the [upstream workflow example](https://github.com/yannh/kubeconform#github-workflow).
The preparation step writes manifests, release-matched CRD schemas, and the target
Kubernetes version into ignored `.generated/gitops/` so the container can read them
from the mounted workspace. It does not install a kubeconform binary on the runner.

Install kubectl, Helm, and kubeconform versions matching `.github/workflows/kubernetes.yml`,
then install the pinned YAML parser in the same Python environment as above:

```sh
.venv/bin/python -m pip install -r kubernetes/requirements.txt
.venv/bin/python -m scripts.check_gitops
KUBECTL_BIN=kubectl HELM_BIN=helm .venv/bin/python -m unittest discover -s scripts/tests -p test_gitops.py -v
```

Validation renders each `kubernetes/**/kustomization.yaml` and checks Application
paths, repository allowlists, and destination namespaces against the shared AppProject.
Each service is built with `kubectl kustomize --enable-helm`, including its pinned
chart, local values, CRDs, and extra manifests together. Validation builds a fresh
temporary copy, excluding chart caches, so it neither mutates service directories
nor accidentally validates a previously downloaded chart version. Manual builds
can leave ignored `charts/` caches; clear those after changing a chart pin.
Custom resource schemas, including Argo's, come directly from the rendered CRDs.
Kubeconform fetches native Kubernetes schemas for the pinned K3s version. The CRD API schema comes from that Kubernetes release's
OpenAPI document; custom resource schemas come from the rendered CRDs. No schemas
are silently skipped. These are public downloads; validation does not use a
kubeconfig, authenticate to the repo, or contact hosts.
`LUA_BIN=lua` additionally executes the Infisical readiness health check against
pending, stale, failed, and successful reconciliation fixtures.
`ARGOCD_BIN=argocd` also checks the rendered SSO policy with Argo's local RBAC
engine: `NullMonkeys` can read, sync, and delete applications; an unrelated
subject cannot. These checks use a local policy file and do not contact a cluster.
See [GitOps setup](gitops.md) for live reconciliation and UI access.

## Terraform

Install the exact TFLint release in `.tflint.hcl` before running these checks.
The workflow reads that same `required_version` pin when installing TFLint, and
Renovate updates it through the custom manager in `renovate.json`. Keep the
requirement in `= X.Y.Z` form; no separate workflow version needs updating.

These commands mirror the Terraform PR workflow:

```sh
terraform fmt -check -recursive terraform
tflint --init
tflint --recursive --format compact
terraform -chdir=terraform/live init -backend=false -input=false -no-color
terraform -chdir=terraform/live validate -no-color
terraform -chdir=terraform/live test -no-color
for module in terraform/modules/*; do
  terraform -chdir="$module" init -backend=false -input=false -no-color
  terraform -chdir="$module" test -no-color
done
```

The checked-in `.tftest.hcl` suites mock providers. Preserve that property so local
tests do not provision resources. Do not substitute a remote plan/apply for a local
validation failure. If initialization or provider execution is blocked by the
environment, report the failed check and its cause.

## Choosing checks

| Change                        | Relevant verification                                                                         |
| ----------------------------- | --------------------------------------------------------------------------------------------- |
| HCP orchestration             | Python suite; ordering, failure propagation, preflight, commit/archive behavior               |
| Inventory/output schema       | Python suite with Ansible integration, plus affected Terraform suites                         |
| Host preparation/storage      | Python suite with Ansible integration; disk-safety fixtures and playbook syntax/targets       |
| K3s configuration             | Python suite with Ansible integration; address/bootstrap guards and actual template rendering |
| GitOps/bootstrap/manifests    | Python suite with Ansible/Kustomize integration and `python -m scripts.check_gitops`          |
| Terraform inputs or resources | Formatting, TFLint, root validation, affected mocked suites                                   |
| GitHub workflows              | Event/job/dependency/concurrency review and affected checks above                             |
| Documentation                 | Relative links, referenced paths/commands against implementation, `git diff --check`          |

Local checks do not establish live OCI/HCP permissions or network reachability.
For API preflight, deployment, or inventory generation against HCP, use the
[deployment runbook](hcp-deployment.md) and [Ansible guide](../ansible/README.md).
