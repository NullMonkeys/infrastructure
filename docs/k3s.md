# K3s installation and network verification

The Deployment workflow runs `ansible/playbooks/site.yml` after inventory is built.
Full deployments first apply every HCP workspace; host-only updates verify the
current applied Terraform inputs and skip those applies. Host preparation
runs first, then K3s installation and verification, followed by
[Argo CD bootstrap](gitops.md) when selected. See [deployment stages](hcp-deployment.md).

## Configuration and order

Edit `ansible/vars/k3s.yml` for cluster settings. The initial pin is
`v1.36.5+k3s1`, selected from the K3s stable channel. The official installer is
downloaded from that release tag and checked against its pinned SHA-256; it also
verifies the downloaded K3s binary. Update the version and installer checksum together.

Inventory `control_plane` hosts run servers with embedded etcd; `workers` run
agents. An odd number of servers is required. One server is allowed for experiments;
three or more are needed for etcd availability through a server failure.

The bootstrap host defaults to the first sorted control-plane hostname. It starts
with `cluster-init: true`; its generated secure node token is copied to other hosts
as a root-owned `0600` file with logging disabled. Remaining servers join one at a
time, followed by workers. The bootstrap host must stay stable on later runs. If
inventory changes would select a fresh host while other servers contain etcd data,
installation stops; set `k3s_bootstrap_host` to an existing server explicitly.

Servers use the same pod/service CIDRs, DNS settings, disabled components, and
secrets encryption. Defaults are pod CIDR `10.42.0.0/16`, service CIDR
`10.43.0.0/16`, and DNS IP `10.43.0.10`. Check these against **all VCN and routed
network CIDRs** before first installation: the offline validation detects overlap
between cluster ranges and conflicts with node IPs, but inventory does not contain
the complete routed network registry. Changing cluster CIDRs later needs an
explicit migration or rebuild.

Bundled Traefik, ServiceLB, and local-path storage remain disabled. Argo installs
Traefik separately with host-network ports 80/443; do not enable the bundled copy.
See [GitOps ingress](gitops.md) for configuration and certificates. K3s services require the prepared
data mounts before starting, so a missing data disk does not silently redirect
future Longhorn data onto the boot filesystem.

K3s advertises the inventory `ansible_host` as `node-external-ip` when it differs
from `private_ip`. Node, Flannel, and API advertise addresses stay private. The
readiness check verifies both InternalIP and ExternalIP against inventory so
[ExternalDNS](external-dns.md) can publish public ingress targets.

## Container image cleanup

Ansible manages kubelet's `10-image-gc.conf` drop-in under
`/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/` on servers and workers. Edit the
four `k3s_image_gc_*` variables in `ansible/vars/k3s.yml` to change the policy.
Unused-image garbage collection starts at 70% image-filesystem usage and tries to
reduce usage to 60%. Images must be unused for at least ten minutes before normal
collection; age-based collection also removes images unused for 24 hours even
without disk pressure. Images used by running containers are retained. Kubelet
does not persist age tracking across restarts, so the 24-hour timer starts again.

The role restarts K3s when the drop-in changes, using the existing ordered server
deployment. This policy concerns containerd's image cache on the boot disk, not
Longhorn volume data. It does not install an external prune cron job. No immediate
space reclamation is promised: kubelet makes collection decisions after restart.
See [K3s drop-ins](https://docs.k3s.io/installation/configuration#kubelet-configuration-drop-in-files)
and [image garbage collection](https://kubernetes.io/docs/concepts/architecture/garbage-collection/).

## Private IPv4 networking and metrics

Each node uses its inventory `private_ip` as its Kubernetes InternalIP. Flannel
uses VXLAN on the interface owning that address. IPv6 remains disabled by host
preparation. This relies on working private routing between tenancies: SSH over
public IPs alone does not establish cluster connectivity.

The existing OCI policy permits traffic among configured node subnets. Required
private traffic includes TCP 6443 for the API, TCP 2379–2380 between etcd servers,
TCP 10250 for kubelets, UDP 8472 between nodes for VXLAN, and TCP 2381 for metrics.
Keep these inside the private network. The role preserves OCI firewall chains and
does not expose cluster ports to the internet.

Every server sets `etcd-expose-metrics: true`. Metrics are available on
`http://<server-private-ip>:2381/metrics` as well as loopback. Verification fetches
each private endpoint and requires an etcd leader metric. This enables the endpoint;
The monitoring stack discovers these private endpoints; see [monitoring](monitoring.md).

## Verification and reruns

After installation, Ansible waits for every inventory node to be Ready with the
expected private InternalIP, verifies each etcd metrics endpoint, and waits for
CoreDNS. It creates a temporary namespace containing a ClusterIP service and one
small HTTP pod per node. Checks exercise:

- DNS and ClusterIP access from every node's pod.
- Direct HTTP between pods on every ordered pair of different nodes, including a
  64 KiB response to help reveal data-path problems that a TCP connection alone misses.

The namespace and temporary manifest are removed after checks, including on failure.
An unrelated existing namespace is never reused or deleted. These checks establish
the tested pod/service paths; they do not measure performance or prove failover.

A failure leaves the installed cluster in place. Fix the private routing, firewall,
or host issue, then rerun deployment. The role reuses the existing etcd data and
does not reset or uninstall nodes automatically. It refuses unmanaged configuration
and implicit server/worker role changes. Routine reruns retain the generated token
and avoid restarting services when installation/configuration is unchanged.

On a server, inspect the cluster with:

```sh
sudo k3s kubectl get nodes -o wide
sudo k3s kubectl -n kube-system get pods -o wide
sudo journalctl -u k3s -n 100 --no-pager
curl --fail http://127.0.0.1:2381/metrics
```

Workers use service `k3s-agent` for journal inspection. The administrative kubeconfig
stays at `/etc/rancher/k3s/k3s.yaml` with mode `0600`; join tokens and kubeconfigs are
not uploaded as Actions artifacts. The existing SSH secret is sufficient.

References: [embedded-etcd HA](https://docs.k3s.io/datastore/ha-embedded),
[server flags and etcd metrics](https://docs.k3s.io/cli/server),
[network requirements](https://docs.k3s.io/installation/requirements),
[Flannel options](https://docs.k3s.io/networking/basic-network-options),
[pinned release](https://github.com/k3s-io/k3s/releases/tag/v1.36.5%2Bk3s1).
