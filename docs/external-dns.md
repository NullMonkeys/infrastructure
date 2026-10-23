# Automatic public DNS

Argo installs the pinned upstream ExternalDNS chart from
`kubernetes/platform/external-dns`. It manages A and CNAME records in
`nullmonkeys.com` through Cloudflare. Cert-manager continues to own ACME TXT
challenges and certificates.

## Address discovery

Ansible sets K3s `node-external-ip` from each host's inventory `ansible_host`,
while `node-ip`, Flannel, and the API advertise address remain private. The K3s
readiness check requires that public address to appear in Node status.

The `traefik/traefik-dns` headless Service selects the existing Traefik pods.
ExternalDNS reads its ready EndpointSlices and uses `NodeExternalIP` to create
`ingress.nullmonkeys.com` A records for the nodes hosting those ready pods.
No public IPs are hard-coded. Missing external addresses produce no targets
rather than falling back to private addresses. Private and CGNAT target networks
are also excluded from DNS publication.

Traefik publishes `ingress.nullmonkeys.com` in standard Ingress status through
`providers.kubernetesIngress.ingressEndpoint.hostname`. This is needed for Argo's
normal Ingress health assessment: without it, the host-network controller has no
LoadBalancer Service address to publish and applications remain Progressing even
when their pods are ready, blocking later sync waves.

Traefik remains a three-replica host-network Deployment. DNS follows pod placement,
so changing it to a DaemonSet is unnecessary. Endpoint readiness is not an
end-to-end HTTPS health check; reconciliation runs every minute and cached DNS
answers can persist for the configured 300-second TTL. If all endpoints disappear,
`policy: sync` removes the owned ingress records.

The service Ingresses and Argo's IngressRoute carry this annotation:

```yaml
external-dns.kubernetes.io/target: ingress.nullmonkeys.com
```

ExternalDNS reads their host rules and creates CNAMEs for Argo, Authentik, Grafana,
and Longhorn. New services need the same annotation and a host under
`nullmonkeys.com`. DNS publication does not wait for an application's certificate
or backend to become healthy.

## Credentials

In the existing Infisical project `acbf89d5-1845-4cbe-9e2d-e623806693e8`, production
environment `prod`, create `/k3s/external-dns` with:

| Key                    | Value                                                                         |
| ---------------------- | ----------------------------------------------------------------------------- |
| `cloudflare-api-token` | Cloudflare API token scoped to `nullmonkeys.com`, with Zone Read and DNS Edit |
| `cloudflare-zone-id`   | The zone ID shown in Cloudflare's domain overview                             |

Grant the existing Infisical machine identity read access to this additional
path. A dedicated Cloudflare token allows independent rotation from cert-manager.
The operator syncs these into `external-dns/external-dns-secrets` every hour;
neither value is stored in Git. Restart ExternalDNS after token rotation because
it reads the token from its environment at startup.

The zone ID is supplied to `--zone-id-filter` through a Secret-backed environment
variable. Together with `domainFilters`, it restricts provider requests to the
configured zone. ExternalDNS uses one replica and read-only Kubernetes RBAC.

## Rollout and existing DNS records

These deployment steps require authorization for live operations:

1. Populate Infisical and grant the machine identity access before deployment.
2. Run normal deployment after merging. The Ansible changes select host/K3s
   configuration, which advertises public node addresses; Argo reconciles the
   Kubernetes manifests independently. Verify both before switching DNS.
   If the root Application has a running sync pinned to an older revision,
   recover that operation first; a successful host workflow does not establish
   that ExternalDNS has been created.
3. Verify `external-dns` is reconciled and `ingress.nullmonkeys.com` contains
   exactly the public addresses of nodes running ready Traefik pods.
4. Back up existing application DNS records. Existing unmanaged A/AAAA/CNAME
   records are not automatically adopted by the TXT registry. During a planned
   cutover, remove only records being replaced by the four application CNAMEs,
   then let ExternalDNS recreate them with ownership TXT records. This can briefly
   interrupt name resolution. Leave unrelated names, mail, and ACME TXT records
   untouched.
5. Verify application DNS, TLS, and backend responses. DNS alone does not repair
   an application that has not deployed.

The TXT registry uses owner `nullmonkeys-k3s` and prefix `external-dns-`, avoiding
TXT/CNAME conflicts. `policy: sync` updates and deletes records owned by this
controller as Kubernetes resources change. Do not change the owner ID or prefix
without a migration. To preview provider changes, temporarily add `--dry-run` to
`extraArgs`, inspect logs, and remove it before the DNS cutover.

All managed A/CNAME records use Cloudflare proxying, including the shared ingress
name. Use Full (strict) TLS and verify origin certificates; TXT ownership and
cert-manager's ACME challenges remain DNS-only. ExternalDNS v0.23 requires the
bare `--cloudflare-proxied` flag: appending `=true` or `=false` causes a parsing
error. Reconciliation restores proxy settings if an owned record is changed
manually in Cloudflare. This controller does not adopt or change unrelated
existing records in the zone.

Longhorn enables both `preUpgradeChecker.jobEnabled` and manager-side upgrade
version checks. Kustomize converts the chart's Helm pre-upgrade Job into an Argo
`Sync` hook at wave `-1`. CRDs and RBAC are ordinary resources at wave `-2`, so
the check can start on a fresh cluster; workload resources stay at wave `0` and
wait for it to pass. This changes the upstream hook ordering deliberately:
prerequisite CRDs/RBAC can update before the check, but workload updates wait.
Successful checker Jobs are removed; failed Jobs remain available for inspection
and are replaced before the next attempt.

A running operation from before this fix can still be stuck on the old PreSync
hook. Under an authorized recovery, terminate the stale root and Longhorn
operations and reconcile the fixed revision. Do not delete storage resources
or namespaces to recover a checker Job.
ExternalDNS is at wave `-9`, after Infisical and Traefik at `-10`.

## Inspect without exposing credentials

On a control-plane node:

```sh
sudo k3s kubectl get nodes -o wide
sudo k3s kubectl -n traefik get pods -o wide
sudo k3s kubectl -n traefik get endpointslices -l kubernetes.io/service-name=traefik-dns
sudo k3s kubectl -n external-dns get infisicalstaticsecrets,deployments,pods
sudo k3s kubectl -n external-dns logs deployment/external-dns --tail=100
```

For rollback, stop reconciliation of the ExternalDNS Application, stop its
Deployment, and restore the backed-up DNS records. Do not delete the headless
Service or application routes while ExternalDNS is still syncing: it can delete
their owned records. Restoring a direct node A record requires verifying that
node actually hosts a ready Traefik pod.

References: [Cloudflare provider](https://github.com/kubernetes-sigs/external-dns/blob/v0.23.0/docs/tutorials/cloudflare.md),
[headless Service annotations](https://github.com/kubernetes-sigs/external-dns/blob/v0.23.0/docs/annotations/annotations.md#external-dnskubernetesioendpoints-type),
[Traefik source](https://github.com/kubernetes-sigs/external-dns/blob/v0.23.0/docs/sources/traefik-proxy.md).
