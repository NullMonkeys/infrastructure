# Pelican Panel

The `pelican-panel` Argo Application deploys the panel at
`https://pelican.nullmonkeys.com` with CloudNativePG and a standalone Redis instance.

Within the Application, Infisical secrets synchronize at wave `-2`. PostgreSQL and
all Redis chart resources run together at wave `-1`, followed by the panel PVCs
and Service at wave `0`, then the panel Deployment and Ingress at wave `1`.

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
