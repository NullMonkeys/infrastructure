# Inventory from HCP Terraform

See [AGENTS.md](../AGENTS.md) for repository contracts and
[development checks](../docs/development.md) for offline verification.

Full deployments build inventory after **all configured HCP workspaces** finish
successfully. Host-only deployments instead verify and fetch the existing applied
outputs without queueing HCP runs; see [stage selection](../docs/hcp-deployment.md).
The workflow publishes inventory as an artifact for later Ansible
automation. A dependent host-configuration job then runs
[`playbooks/site.yml`](playbooks/site.yml): disable IPv6 at kernel level, install
packages, prepare data storage, install K3s, and verify cluster networking and etcd
metrics, then bootstrap Argo CD when that stage is selected. Hosts reboot one at a time
when needed to activate the IPv4-only policy.
See [host preparation](../docs/host-preparation.md) and
[K3s configuration](../docs/k3s.md). [GitOps setup](../docs/gitops.md) covers Argo
repository access, ownership, and the web UI. Argo manages the Longhorn manifests.

Terraform publishes three root outputs:

- `inventory`: account, deployed commit, and hosts keyed by unique Linux hostname.
- `resources`: instance and volume OCIDs keyed by the same hostname.
- `network`: the network IDs, including the shared DRG used by peer workspaces.

The old `compute_nodes` output is replaced by `inventory` and `resources`. The input
variable `compute_nodes` is unchanged. Example of one workspace's `inventory` output:

```hcl
inventory = {
  account = "example"
  commit  = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  hosts = {
    example-control-plane-1 = {
      role       = "control-plane"
      public_ip  = "203.0.113.10"
      private_ip = "10.1.1.10"
      block_volumes = {
        data = {
          device          = "/dev/oracleoci/oraclevdb"
          attachment_type = "paravirtualized"
          is_read_only    = false
        }
      }
    }
  }
}
```

The deployment script supplies `deployment_commit` as a run-specific Terraform
variable in each workspace. Inventory generation refuses to combine outputs from
different commits or to silently omit a workspace. It uses the HCP output API,
not downloaded state files, and copies only the non-sensitive `inventory` output.
For host-only updates, `--reuse-applied --commit <configuration-sha>` additionally
checks idle workspaces and compares committed Terraform inputs against the common
applied SHA. Inventory retains that actual SHA as `deployment_commit` and records
the host code SHA as `configuration_commit`. Missing, mixed, or incompatible
outputs fail rather than falling back to an old artifact. Full deployments require
an exact output match with the current commit as before.

The generated file is `.generated/ansible/inventory.json`. JSON is valid input to
Ansible's YAML inventory plugin. It is ignored by Git and saved as the Actions
artifact `ansible-inventory-<commit>` for seven days. Re-running deployment rebuilds
the inventory, including any changed public IPs.

Groups:

```text
all
├── k3s_cluster
│   ├── control_plane
│   └── workers
└── example
```

The `example` group represents the `account` value from one workspace's output;
each configured workspace contributes its own group.

Host aliases match Linux hostnames, for example `example-control-plane-1`.
Each host has `ansible_host` (public IPv4), `private_ip`, `account`, `node_role`, and
`hcp_workspace`, and `block_volumes` (including an empty map for nodes without data
volumes). An empty account is allowed, but an entirely empty inventory is rejected.

## Local use

Install the pinned Ansible version with Python 3.12 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r ansible/requirements.txt
.venv/bin/ansible-galaxy collection install -r ansible/collections/requirements.yml
```

Set `HCP_TERRAFORM_TOKEN` in your environment to a user/team token with access to
read workspaces and their outputs. After all workspaces have applied the new output
schema, generate inventory for the expected deployed commit:

```sh
python3 -m scripts.build_inventory --commit "$(git rev-parse HEAD)"
ANSIBLE_CONFIG=ansible/ansible.cfg .venv/bin/ansible-inventory --graph
ANSIBLE_CONFIG=ansible/ansible.cfg .venv/bin/ansible-inventory --list
```

The `--output` option can select a different generated file. When using it, pass that
path explicitly with `ansible-inventory -i <path>`.

PR checks run inventory tests against mocked HCP outputs and parse the resulting
file with Ansible, check the deployment playbook's syntax and targets, render K3s
configuration templates, and test storage safety checks with synthetic disk information. No HCP token, OCI
credentials, SSH key, or running hosts are needed for those checks.

References: [HCP output API](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/state-version-outputs),
[Ansible inventory](https://docs.ansible.com/projects/ansible/latest/inventory_guide/intro_inventory.html).
