# Pelican Panel

The `pelican-panel` Argo Application deploys the panel at
`https://pelican.nullmonkeys.com` with CloudNativePG and a standalone Redis instance.
An additional MariaDB instance hosts databases for the game servers managed by
Wings. The panel itself continues using PostgreSQL.

Within the Application, Infisical secrets synchronize at wave `-2`. PostgreSQL,
MariaDB, and all Redis chart resources run together at wave `-1`, followed by the
MariaDB user/grant, panel PVCs, and Service at wave `0`, then the panel Deployment
and Ingress at wave `1`.

## Game server databases over Tailscale

The MariaDB operator creates Service `pelican-panel/pelican-mariadb` using
`loadBalancerClass: tailscale`, with NodePort allocation disabled. Its Service
template sets `tailscale.com/hostname: pelican-mariadb`, so the existing Tailscale
operator provides private TCP access on port `3306`. This follows the
[Tailscale Service ingress configuration](https://tailscale.com/docs/kubernetes-operator/ingress).
The Service remains accessible inside Kubernetes at
`pelican-mariadb.pelican-panel.svc.cluster.local:3306`.

Populate `mariadb-root-password` and `mariadb-pelican-password` in the existing
Infisical path `/k3s/pelican-panel`, environment `prod`. They synchronize into
`pelican-panel-secrets`; no MariaDB passwords are generated or stored in Git.
The `pelican-admin` SQL user uses `mariadb-pelican-password` and has the configured
global privileges with grant option so Pelican can manage server databases/users.

Wings machines must join the same tailnet. The proxy uses the operator's default
`tag:k8s` device tag; tailnet access policy must permit the Wings identities to
reach that tag on `tcp:3306`. Tag ownership alone does not grant traffic access.
The expected MagicDNS hostname is `pelican-mariadb`; use the actual full
`pelican-mariadb.<tailnet>.ts.net` name or its Tailscale IP for clients outside
Kubernetes. Tailscale can suffix a name if another device already uses it.

After an authorized rollout, inspect the generated Service and proxy:

```sh
kubectl -n pelican-panel get mariadb pelican-mariadb
kubectl -n pelican-panel get service pelican-mariadb -o wide
kubectl -n tailscale get pods -l tailscale.com/parent-resource=pelican-mariadb
```

From a Wings machine, test TCP connectivity with
`nc -vz pelican-mariadb 3306`. Also test from a game container: its network must
route to the tailnet, and its DNS must resolve the chosen hostname. Using the
Tailscale IP avoids container DNS dependencies for those clients.

In Pelican's **Databases → New Database Host**, use host `pelican-mariadb`, port
`3306`, username `pelican-admin`, and the password from `mariadb-pelican-password`.
The short hostname deliberately matches both the Kubernetes Service and the
MagicDNS name: the panel resolves it inside its Kubernetes namespace, while Wings
and its containers resolve it through Tailscale. Pelican rc.2 uses one host field
for both administration and the endpoint shown to users, so a Kubernetes-only
FQDN would not work for game containers. A tailnet FQDN/IP in that field would
instead require tailnet egress from the panel. Confirm that MagicDNS registered
the exact short hostname before configuring the database host.

MariaDB requests a separate 5Gi Longhorn volume and exports database metrics for
the provisioned Grafana dashboard. Backups for this MariaDB instance are not yet
configured. Local checks do not verify tailnet policy, registration, or live
connections from Wings or its containers.

## Storage and updates

The panel runs one replica with the `Recreate` update strategy, so updates stop the
old pod before starting its replacement. This prevents overlapping startup
migrations and schedulers and allows its RWO volumes to move between nodes.
Updates cause a brief outage.

The panel requests three Longhorn RWO claims: 5Gi for data, 2Gi for logs, and 1Gi
for plugins. Redis requests a separate 512Mi RWO claim to retain queued jobs and
sessions across pod replacement. Longhorn's configured two storage replicas mean
the Redis claim can consume roughly 1Gi before snapshot overhead. The panel's
`fsGroup` gives its non-root `www-data` process access to the RWO volumes.

Existing bound RWX claims cannot be converted to RWO by editing the manifest.
Preserve their data and plan a migration before syncing the access-mode change.

## Initial administrator

`APP_INSTALLED=true` deliberately disables the web installer. After the panel is
running, create the first administrator interactively:

```sh
kubectl -n pelican-panel exec -it deployment/pelican-panel -- php artisan p:user:make
```

Choose administrator access when prompted. Enter the password at the prompt so it
does not appear in shell history.

## Health checks

Startup, readiness, and liveness probes use the image's `/up` HTTP endpoint.
Startup allows up to ten minutes for database availability and migrations before
Kubernetes restarts the container. Readiness gates Service traffic, and liveness
detects a failed web process after startup completes.

The panel uses `/k3s/pelican-panel` in Infisical. Preserve a stable `APP_KEY` when
restoring its database; the PostgreSQL backup does not include panel data or
plugins stored in the other claims.
