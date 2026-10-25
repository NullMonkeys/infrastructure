# GitOps with Argo CD

This repository holds provisioning, host setup, and Kubernetes desired state.
Argo reads `main` from the same repository and manages its own installation.

## Ownership and layout

```text
kubernetes/
├── bootstrap/
│   ├── applications/   # One Argo Application manifest per service
│   ├── projects.yaml   # One shared infrastructure project
│   └── ...             # Namespace, rendering support, root Application
├── platform/
│   ├── argocd/
│   ├── cert-manager/
│   ├── external-dns/
│   ├── infisical/
│   ├── cnpg/
│   ├── longhorn/
│   ├── mariadb-operator/
│   ├── tailscale/
│   └── traefik/
└── workloads/
    ├── authentik/
    ├── kube-prometheus-stack/
    └── pelican-panel/
```

Each service directory has a `kustomization.yaml` listing its resources and, where
needed, a pinned `helmCharts` entry using its local `values.yaml`. Each Application
has a single Git source pointing at that service directory. For example,
`bootstrap/applications/infisical.yaml` points at `kubernetes/platform/infisical`;
that folder includes the operator and its connection/authentication resources together.

The root Application `cluster` watches only `kubernetes/bootstrap/applications/`. Infisical
and Traefik start at wave `-10`, ExternalDNS, cert-manager, Longhorn and Tailscale at
`-9`, monitoring at `-8`, CNPG and MariaDB operator at `-7`, Authentik at `-2`,
Argo self-management at `-1`, and Pelican at `0`.
Bootstrap seeds missing ServiceMonitor/PodMonitor CRDs before root reconciliation,
so each service can enable its chart-provided monitor. Monitoring manages the full
CRD set; Longhorn is ready before monitoring requests persistent volumes.
Inside cert-manager, the Cloudflare secret sync is at resource wave `0` and
the issuer at `1`. This puts a service's dependent resources in the same Application.
The InfisicalStaticSecret health check waits for successful synchronization.
Child Application health propagates to the root.

See [monitoring](monitoring.md) for storage budgets, retention, metrics discovery,
and local validation.
See [Tailscale](tailscale.md) for operator credentials and tailnet registration.
See [Authentik recovery](authentik.md) for the existing database restore source
and the separate archive used by the restored cluster.
See [Pelican Panel](pelican-panel.md) for its storage budget, update behavior,
health checks, and initial CLI administrator setup.

All Applications, including the root, use the single `infrastructure` AppProject.
It accepts manifests from this repository and permits any namespace and resource
type in this cluster. `platform/` and `workloads/` are organizational folders;
there are no separate permission policies for them.

## Initial bootstrap and self-management

After K3s verification, Ansible seeds the official Argo installation only if
`deployment/argocd-server` is absent. It seeds child Application health checks
and applies the bootstrap namespace, projects, and root Application. Argo then
adopts the installation with server-side apply.
Normal Ansible reruns leave the installation and its settings to Argo, avoiding
conflicts with Git-managed upgrades and ingress configuration.

`kubernetes/platform/argocd/kustomization.yaml` holds the upstream release URL (first
resource). Ansible and Kustomize read the same release URL; update it when
upgrading Argo. Each other service's `kustomization.yaml` pins
its Helm chart; `values.yaml` is beside it. The initial Argo installation remains
non-HA.

Ansible applies `bootstrap/rendering.yaml` before root reconciliation to enable
Kustomize's `--enable-helm` support, as described in the [Argo documentation](https://argo-cd.readthedocs.io/en/stable/user-guide/kustomize/#kustomizing-helm-charts).
Bootstrap owns only `argocd-cm.data["kustomize.buildOptions"]`; Argo owns the other
ConfigMap settings. A distinct bootstrap field manager preserves legacy fields
during migration. Argo's Application ignores this one bootstrap-owned field when
comparing and syncing its ConfigMap. Normal runs do not reapply the upstream Argo
installation.

Argo reads the public repository at
`https://github.com/NullMonkeys/infrastructure.git` over HTTPS without credentials.
The root and child Applications and the AppProject allowlist use this same URL.
No repository deploy key, repository credential Secret, or GitHub Actions secret
is required for Argo. The repository must be public before bootstrapping the
cluster. Ansible still uses a separate node SSH key to configure hosts.
Ansible also bootstraps the Infisical machine identity before Argo starts. Infisical
supplies the Cloudflare token to cert-manager; see [secret management](secrets.md).

Bootstrap seeds missing monitor API prerequisites and applies the root Application,
then waits for Argo's own controllers.
Child Application reconciliation proceeds independently and does not hold up
Actions. A successful bootstrap confirms that Argo is running and configured;
inspect application sync and health in Argo for workload deployment results.
Ordinary service manifests and child Application definitions need only a push to
`main`; no Ansible run or explicit Argo sync is required. Top-level bootstrap
manifests remain Ansible-owned and still select bootstrap when changed.
On the first migration, the shared `infrastructure` project is applied by Ansible;
Argo may report it missing until that pipeline step completes.

## Public access and certificates

The UI and native Argo CLI use **https://argocd.nullmonkeys.com**. Argo's Service
stays ClusterIP. Traefik terminates TLS and forwards HTTP to Argo; a separate,
higher-priority route forwards native gRPC using h2c. Authentik provides OIDC
through Argo's bundled Dex; see [SSO configuration](#authentik-sso).

Cert-manager requests and renews a Let's Encrypt production certificate using
Cloudflare DNS-01, registered to `no-reply@nullmonkeys.com`. The Cloudflare API token
is synced from Infisical Cloud into `cert-manager/cert-manager-secrets`.
Cloudflare can proxy the public hostname; use **Full (strict)** origin TLS after
the certificate is Ready. DNS-01 works without HTTP challenge routes. Public TCP
80/443 remain for redirects and HTTPS; no NodePort range or LoadBalancer Service
is required. Bundled K3s Traefik and ServiceLB remain disabled.

Traefik retains the three-replica host-network Deployment and uses
`ClusterFirstWithHostNet` DNS. Explicit anti-affinity avoids port
collisions between replicas. Origins must be nodes actually running Traefik;
fixed replicas do not provide ingress on every node. ExternalDNS follows ready
Traefik endpoints through a headless Service and manages the shared public ingress
name and application CNAMEs; see [automatic DNS](external-dns.md) for credentials,
ownership handover, and rollout. Traefik runs as UID 0 so its
host-network listeners can bind ports 80/443 with this runtime. All capabilities
are dropped except `NET_BIND_SERVICE`; the filesystem stays read-only and
privilege escalation stays disabled. The non-root image can otherwise exit with
`bind: permission denied` despite that capability being requested; see the
[upstream privileged-port notes](https://github.com/traefik/traefik-helm-chart/blob/master/EXAMPLES.md#bind-to-privileged-ports-80-and-443).
For forwarded client IPs,
only Cloudflare's published IPv4 proxy networks are trusted. PROXY protocol and
blanket header trust remain disabled. Enable Cloudflare's gRPC support for the
native Argo CLI, or use `--grpc-web`.

See [Infisical and Cloudflare setup](secrets.md) for the existing project/path,
credential bootstrap, token permissions, proxy settings, and verification.

Retrieve the initial administrator password privately on a control-plane node:

```sh
sudo k3s kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 --decode
```

Sign in as `admin`, change the password, then remove the initial-password Secret
when no longer needed. Bootstrap never prints or resets the password.

For emergency access without public DNS/TLS, keep this SSH tunnel open:

```sh
ssh -t -L 8080:127.0.0.1:8080 ubuntu@<control-plane-public-ip> \
  'sudo k3s kubectl -n argocd port-forward --address 127.0.0.1 svc/argocd-server 8080:80'
```

Open `http://localhost:8080`; the tunnel encrypts the connection. This HTTP backend
is intentional after TLS termination moves to Traefik.

## Authentik SSO

Argo uses the existing Authentik instance at `https://auth.nullmonkeys.com`.
The bundled Dex connector supports both browser and CLI login with the same
confidential OAuth2 client. No Argo users, custom roles, or UI-created settings
are needed. `argocd-rbac-cm` maps the existing `NullMonkeys` group to Argo's
built-in `role:admin`, granting unrestricted access. Other SSO users receive no
default permissions, and anonymous access is disabled.

In the existing Infisical project, environment `prod`, populate `/k3s/argocd`:

| Key             | Value                                   |
| --------------- | --------------------------------------- |
| `client-id`     | Authentik Argo CD provider's client ID   |
| `client-secret` | That provider's client secret           |

Give the operator's existing machine identity read access to this path.
`kubernetes/platform/argocd/secrets.yaml` syncs it hourly into the separate
`argocd/argocd-oidc` Secret. Its `app.kubernetes.io/part-of: argocd` label lets
Argo resolve both credential references without putting their values in Git or
overwriting `argocd-secret` (which also holds Argo's signing key and admin password).
The sync runs before Argo's configuration resources. Secret updates are watched
by Argo; a subsequent sync can take up to an hour after an Infisical rotation.

The Authentik application/provider must match these settings:

| Setting                | Value                                                    |
| ---------------------- | -------------------------------------------------------- |
| Application slug       | `argocd`                                                 |
| Provider type          | OAuth2/OpenID Connect, confidential client                |
| Issuer mode            | Per-provider (default)                                   |
| Issuer                 | `https://auth.nullmonkeys.com/application/o/argocd/`        |
| Authorization redirect | Strict: `https://argocd.nullmonkeys.com/api/dex/callback`   |
| Scope mappings         | `openid`, `profile`, `email`                              |
| Signing key            | An available Authentik signing key                       |
| Application binding    | Existing `NullMonkeys` group only                         |

Keep the default `profile` mapping, which includes the `groups` claim, or an
equivalent mapping that emits `NullMonkeys` in `groups`. Dex explicitly enables
group forwarding. The Authentik binding controls sign-in; the declarative Argo
mapping grants application permissions. The extra Admins/Viewers groups in the
integration guide are optional; this setup uses neither.

This repository configures the Argo side. The existing Authentik provider,
application, group, and access binding are retained in the recovered Authentik
database; they are not created by these manifests. Keep them in the database
backup for a cluster rebuild. If the application's existing slug differs,
update the connector issuer in `kubernetes/platform/argocd/config.yaml` to match.

Authentik reconciles before Argo self-management so Dex's provider discovery
cannot hold up deployment of its own identity provider on a fresh cluster.
The server Deployment follows the SSO ConfigMap at resource wave `1`. Its
`nullmonkeys.com/sso-provider: authentik` pod annotation forces a rollout when
adopting SSO on an existing installation. Argo CD 3.5.6's configuration reload
rebuilds the login handler but retains the session manager created at process
startup. Without a new process, login can succeed and then immediately fail
session validation with `x509: certificate signed by unknown authority` against
the Dex discovery endpoint. Keep the rollout marker and ordering when
rebuilding the cluster; a configuration reload alone is insufficient for this
transition. TLS settings and certificate verification stay unchanged.
The local `admin` account remains available for initial bootstrap and recovery;
it bypasses Authentik's group gate. Its password is managed as described above.

After an authorized deployment, verify the Infisical sync is healthy without
printing credential values, then use **Log in via Authentik** or:

```sh
argocd login argocd.nullmonkeys.com --sso --grpc-web
```

Verify a `NullMonkeys` member can administer applications and a non-member is
rejected by Authentik. Group changes take effect on a new login; existing Dex
sessions keep their group claims until reauthentication.

References: [Authentik Argo CD integration](https://integrations.goauthentik.io/infrastructure/argocd/),
[Argo secret references](https://argo-cd.readthedocs.io/en/stable/operator-manual/user-management/#sensitive-data-and-sso-client-secrets),
[Argo built-in roles](https://argo-cd.readthedocs.io/en/stable/operator-manual/rbac/#basic-built-in-roles).

## Changes, validation, and deletion

PR checks render all local Kustomize roots and the pinned Helm charts, including
CRDs. Kubeconform validates native resources and custom resources against schemas
from those exact releases. The CRD API schema comes from the matching Kubernetes
release. Missing schemas fail validation; no kubeconfig or deployment credentials
are required. See [development checks](development.md).

Changes to Terraform or deployment orchestration run HCP → inventory → Ansible on
`main`. Ansible and top-level bootstrap changes fetch verified applied inventory
without another HCP apply. Node/K3s changes also skip the GitOps bootstrap stage;
see [deployment stage selection](hcp-deployment.md). Changes under
`platform/`, `workloads/`, and nested `bootstrap/applications/` reconcile through
Argo without provisioning. Reconciliation polls every two minutes plus
jitter; no webhook is required.

Argo tracks `main` independently of Actions. Finish infrastructure/host prerequisites
before merging dependent manifests. The Actions concurrency lock does not serialize
Argo reconciliation.

Automatic sync, self-healing, and pruning are enabled in the current Application
manifests. The root prunes removed child Application definitions. Because child
Applications have no cascading-deletion finalizers, deleting their Application
object does not automatically tear down their managed workloads. Remaining service
Applications prune resources removed from their own desired state. Review resource
deletion carefully for Longhorn volumes and the monitoring PVCs. See the
[monitoring storage budget](monitoring.md).

## Migrating from the previous layout

The deployment pipeline updates the root Application's path and enables Helm
rendering. Until that step completes, an existing root may report its previous
source path missing. Resource names and the existing pruning policies are retained.

If the former `infisical-operator` or `certificate-issuers` Applications were already
deployed, disable their auto-sync and retire them **without cascading deletion**
before letting the replacement Applications adopt their resources. Otherwise two
Applications can fight over the same controllers or issuer. The Helm release names
and existing resource names remain unchanged.

The Cloudflare InfisicalStaticSecret now lives in the `cert-manager` namespace.
After the new sync is healthy, explicitly retire an old
`infisical-operator-system/cloudflare-dns` sync resource if present. Check the old
resource's `creationPolicy` before removal: an `Owner` policy can delete its generated
Secret, whereas `Orphan` retains it. The current target is
`cert-manager/cert-manager-secrets`. No live migration or resource deletion is
performed by the repository refactor.

The old `bootstrap`, `platform`, and `workloads` AppProjects may remain in a
previously deployed cluster: Ansible does not prune omitted project objects.
They can be retired once no Application references them. The refactor does not
perform live project deletion.

## Inspect or recover

On a control-plane node:

```sh
sudo k3s kubectl -n argocd get applications
sudo k3s kubectl -n argocd describe application cluster
sudo k3s kubectl -n traefik get deployments,pods
sudo k3s kubectl -n cert-manager get deployments,pods
sudo k3s kubectl get clusterissuer letsencrypt-prod
sudo k3s kubectl -n argocd get certificates,certificaterequests,orders,challenges
sudo k3s kubectl -n argocd describe certificate argocd-public
```

Before issuance completes, Traefik may serve its fallback certificate. Failed
DNS-01 checks need a successful Infisical sync and a Cloudflare token with
zone permissions. Inspect the Challenge before retrying; do not repeatedly delete certificates
or ACME account Secrets, which can consume issuance limits.

For manifest failures, fix Git and retry reconciliation. For bootstrap failures,
fix the cause and rerun deployment; the cluster is retained. If the Argo installation
itself needs reseeding, an explicitly authorized local Ansible run can pass
`-e argocd_recover=true` with the generated inventory to `ansible/playbooks/argocd.yml`.
This reapplies the pinned upstream installation and initial settings, after which
Argo restores Git-managed patches. It can briefly interrupt access; normal reruns
must not set this flag. Existing unmanaged namespaces remain rejected.

References: [Argo self-management](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/#manage-argo-cd-using-argo-cd),
[Traefik ingress for Argo](https://argo-cd.readthedocs.io/en/stable/operator-manual/ingress/#traefik-v30),
[Cloudflare DNS-01 configuration](https://cert-manager.io/docs/configuration/acme/dns01/cloudflare/),
[Traefik Helm chart](https://github.com/traefik/traefik-helm-chart),
[kubeconform workflow](https://github.com/yannh/kubeconform#github-workflow).
