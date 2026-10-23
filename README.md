# Infrastructure

This is our internal infrastructure setup for **NullMonkeys**. It provisions
resources on Oracle Cloud Infrastructure (OCI) and runs our services on K3s.

The repository is public for reference. Its configuration is specific to our
accounts, domains, and services.

GitHub Actions coordinates deployments through HCP Terraform, then runs Ansible
to prepare hosts and install K3s. Argo CD reads this repository over HTTPS and
keeps platform services and workloads in sync with `main`.

## Repository layout

- [`terraform/`](terraform/) — OCI networking, compute, and storage.
- [`ansible/`](ansible/) — Host preparation, K3s installation, and GitOps bootstrap.
- [`kubernetes/`](kubernetes/) — Argo CD applications, platform services, and workloads.
- [`scripts/`](scripts/) — Deployment orchestration, inventory generation, and validation.

## License

[MIT](LICENSE) — Copyright (c) 2026 NullMonkeys.
