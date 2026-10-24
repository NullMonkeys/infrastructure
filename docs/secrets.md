# Infisical Cloud and Cloudflare DNS

The Infisical Kubernetes operator pulls secrets from the existing Infisical Cloud
project. Argo manages the operator and sync definitions; Ansible supplies only the
machine identity credential. Secret values stay out of Git and workflow artifacts.

## Current connection

| Setting                | Value                                                           |
| ---------------------- | --------------------------------------------------------------- |
| Instance               | `https://app.infisical.com`                                     |
| Project                | Default (`acbf89d5-1845-4cbe-9e2d-e623806693e8`)                |
| Environment            | Production (`prod`)                                             |
| Path                   | `/k3s/cert-manager`                                             |
| Infisical key          | `cloudflare-api-token`                                          |
| Kubernetes destination | `cert-manager/cert-manager-secrets`, key `cloudflare-api-token` |
| Refresh                | Every hour                                                      |

These identifiers and paths are configuration, not credentials. Edit
`kubernetes/platform/cert-manager/secrets.yaml` to change the source. The current
sync copies the configured folder into `cert-manager-secrets` without renaming its
keys. Cert-manager reads its `cloudflare-api-token` entry. The sync uses
`creationPolicy: Owner`, so deleting the sync resource also removes its generated
Secret. Preserve that policy and the one-hour refresh interval when moving files.

## Machine identity bootstrap

Use an Infisical machine identity with Universal Auth and read-only access to the
production paths used by consumers: `/k3s/cert-manager`, `/k3s/external-dns`,
`/k3s/authentik`, `/k3s/argocd`, and `/k3s/tailscale`. Restrict its scope
to those paths/environment; do not grant
secret-writing or organization administration permissions. Universal Auth allows
Infisical Cloud to authenticate the operator without reaching the private K3s API.

Save its client credentials as repository secrets in `NullMonkeys/infrastructure`:

- `INFISICAL_CLIENT_ID`
- `INFISICAL_CLIENT_SECRET`

The deployment workflow passes these only to the Ansible step. The `infisical`
role runs before Argo bootstrap, creates `infisical-operator-system`, and applies
`infisical-universal-auth` through stdin with `no_log` and diff output disabled.
It writes no credential file. Normal local reruns may reuse the installed Secret.
When rotating, supply both new values together; a partial pair fails validation.
The Cloudflare token itself remains in Infisical, not GitHub Actions.

The operator uses the `v1beta1` InfisicalConnection, InfisicalAuth, and
InfisicalStaticSecret APIs. Its chart version is pinned in
`kubernetes/platform/infisical/kustomization.yaml`, alongside its values, connection, and auth
manifests. One `infisical` Application owns that whole service. Consumer-specific
sync resources live with their consumers. The chart's default cluster-wide scope
supports later secrets in other namespaces; treat operator and sync manifests as
administrative configuration.
It is not a tenant isolation boundary. The machine identity's Infisical permissions
still determine what it can fetch.

Argo starts the Infisical service and Traefik at wave `-10`, Tailscale, ExternalDNS
and cert-manager at `-9`, Authentik at `-2`, and its own certificate/ingress at `-1`.
Argo's OIDC client credentials come from `/k3s/argocd`; see
[Authentik SSO](gitops.md#authentik-sso) for the keys and provider settings.
Inside cert-manager's Application, the
Cloudflare sync is at resource wave `0` and the ACME issuer follows at `1`.
The custom InfisicalStaticSecret health check waits for a successful reconciliation
of the current generation. On an existing installation the health customization
is adopted when Argo first syncs its updated configuration; controllers retry
missing credentials and dependent certificates in the meantime.

Tailscale syncs `/k3s/tailscale` into `tailscale/operator-oauth` at resource wave
`-1`, before its operator starts. Store the OAuth credentials as `client-id` and
`client-secret`; see [Tailscale setup](tailscale.md) for the tag policy, client
permissions, and mounted filename mapping.

## Cloudflare token and DNS-01

The existing Infisical token must have `Zone → DNS → Edit` and `Zone → Zone → Read`
for **only `nullmonkeys.com`**. If its scope differs, replace it in Infisical.
No account-wide API key is needed.

Cert-manager's `letsencrypt-prod` ClusterIssuer reads the synced token from its
cluster-resource namespace, `cert-manager`, and creates temporary ACME TXT records
in `nullmonkeys.com`. Certificate ownership stays with cert-manager. Traefik reads
`argocd/argocd-public-tls`; it has no ACME certificate resolver of its own.
DNS-01 issuance and renewal do not rely on public HTTP challenge paths.

To enable Cloudflare proxying after deployment:

1. Verify the Infisical sync succeeds and `argocd-public` is Ready.
2. Complete the [ExternalDNS ownership handover](external-dns.md). ExternalDNS
   proxies all its managed A/CNAME records, including Argo's CNAME pointing to the
   shared ingress name.
3. Use Cloudflare **Full (strict)** SSL/TLS mode. Flexible mode causes HTTPS
   redirect problems and does not encrypt the connection to the origin.
4. Enable Cloudflare gRPC support if using the native Argo CLI. The web UI and
   `argocd --grpc-web` can use the ordinary HTTPS route.

Traefik trusts forwarded headers only from Cloudflare's published IPv4 proxy
networks, on both ports 80 and 443. PROXY protocol and blanket header trust remain
disabled. Maintain the allowlist in `kubernetes/platform/traefik/values.yaml` against
[Cloudflare's published list](https://www.cloudflare.com/ips-v4/). Origin hosts stay
IPv4-only; Cloudflare may serve IPv6 to edge clients independently.

This trust list does not restrict who can connect to origin ports 80/443. Direct
origin requests still receive normal Argo authentication and do not gain trusted
forwarded headers. Any later origin firewall restrictions need a separate change.

Traefik retains the configured three-replica host-network Deployment, with
`ClusterFirstWithHostNet` DNS and explicit pod anti-affinity. Only nodes
hosting a Traefik pod accept ingress. ExternalDNS publishes their public node
addresses from ready headless Service endpoints; see [automatic DNS](external-dns.md).

## Verify and troubleshoot

On a control-plane node, inspect metadata and status without dumping Secrets:

```sh
sudo k3s kubectl -n infisical-operator-system get deployments,pods
sudo k3s kubectl -n infisical-operator-system get infisicalconnections,infisicalauths
sudo k3s kubectl -n cert-manager get infisicalstaticsecrets
sudo k3s kubectl -n cert-manager get secret cert-manager-secrets
sudo k3s kubectl -n argocd get certificate argocd-public
sudo k3s kubectl -n argocd get orders,challenges
```

If sync fails, check the production environment slug/path and machine identity
read permissions before rotating credentials. If sync works but issuance fails,
check the Cloudflare token permissions and DNS-01 Challenge status. Do not print
Secret YAML or retrieve token values into Actions logs for diagnostics. ExternalDNS
manages public A/CNAME records separately from cert-manager's ACME TXT records.

References: [Infisical operator](https://infisical.com/docs/integrations/platforms/kubernetes/overview),
[Universal Auth configuration](https://infisical.com/docs/integrations/platforms/kubernetes/infisical-auth-crd),
[static secret configuration](https://infisical.com/docs/integrations/platforms/kubernetes/infisical-static-secret-crd),
[cert-manager Cloudflare DNS-01](https://cert-manager.io/docs/configuration/acme/dns01/cloudflare/).
