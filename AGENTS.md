# Repository Guidelines

## Project Structure & Module Organization

- `terraform/live/` is the shared HCP Terraform root; `terraform/modules/` contains OCI networking and compute modules.
- `scripts/` handles deployment sequencing, inventory generation, and GitOps validation; `scripts/tests/` contains Python tests.
- `ansible/roles/` prepares hosts and installs K3s and Argo CD; `ansible/playbooks/site.yml` is the deployment entry point.
- `kubernetes/bootstrap/` seeds Argo; `kubernetes/platform/` and `kubernetes/workloads/` group services with their chart pins, values, and manifests.
- `.github/workflows/` defines checks and deployment; `docs/` contains operational guides.

## Build, Test, and Development Commands

Run from the repository root:

```sh
python3 -m unittest discover -s scripts/tests -v
terraform fmt -check -recursive terraform
terraform -chdir=terraform/live init -backend=false -input=false
terraform -chdir=terraform/live validate
terraform -chdir=terraform/live test
tflint --recursive --format compact
python3 -m scripts.check_gitops
```

These run Python tests, check Terraform formatting, initialize providers, validate configuration, execute mocked Terraform tests, lint, and render/validate GitOps resources. Follow [docs/development.md](docs/development.md) for pinned dependencies, TFLint initialization, module tests, and integration checks.

## Coding Style & Naming Conventions

Use four-space Python indentation and two-space YAML/HCL indentation; format HCL with `terraform fmt`. Keep deployment/inventory scripts standard-library-only. Use `snake_case` for Python functions and Terraform inputs. Preserve hostnames as `<account>-<compute_nodes key>`: changing keys can replace nodes or detach storage. Keep each Kubernetes service in one folder with one Argo Application.

## Testing Guidelines

Use Python `unittest` with files named `test_*.py` and mocked Terraform suites named `*.tftest.hcl`. Add regression coverage for changed behavior using synthetic credentials and addresses. Set `ANSIBLE_INVENTORY_BIN` for actual Ansible parser/template checks and `KUBECTL_BIN`/`HELM_BIN` for rendering integration tests. Report skipped checks; local tests do not establish live cloud success.

## Commit & Pull Request Guidelines

Use Conventional Commit messages: `<type>: <summary>` or `<type>(<scope>): <summary>`. Common types are `feat`, `fix`, `docs`, `refactor`, `test`, `ci`, and `chore`. The optional scope identifies the affected service or component. Keep summaries concise and imperative.

Examples:

- `feat: add worker nodes`
- `fix(authentik): sync credentials before database recovery`
- `docs: clarify deployment prerequisites`

PRs should explain behavior, operational impact, relevant issues, and checks run or skipped. Update affected guides and pass Terraform, Ansible, and Kubernetes CI checks as applicable.

## Security & Deployment Boundaries

Keep credentials, state, plans, kubeconfigs, and generated inventory out of Git. Preserve host-key checking and secret redaction. GitHub Actions sequences HCP deployment and Ansible; Argo reconciles service manifests independently. Obtain authorization before live deployment or host operations.
