# Authentik database recovery

The active Kustomize overlay is `postgres/patch-recovery.yaml`. It restores the
existing `authentik` database and owner from the `authentik-db` archive in
`s3://nm-authentik-pg` on Backblaze B2. It does not initialize an empty database.

The restored cluster writes its new WAL files and backups to the separate
`authentik-db-restored-20261023` server prefix in the same bucket. The original
source archive remains unchanged. Credentials come from the existing
`/k3s/authentik` Infisical path and must permit access to both prefixes.

CloudNativePG requires the destination archive to be empty at bootstrap. Using
the original server prefix for both recovery and backup fails with
`Expected empty archive`. Keep that safeguard enabled; do not delete the source
WAL files or set `cnpg.io/skipEmptyWalArchiveCheck` to bypass it.

For a future restore, select the desired source prefix in
`externalClusters[].barmanObjectStore.serverName` and a new, unused destination
prefix in `backup.barmanObjectStore.serverName`. The date suffix identifies this
restore incarnation; do not reuse it for another fresh cluster. Normal restarts
of this existing cluster retain their configured destination prefix.

After changing a failed bootstrap configuration, allow Argo to sync the Cluster
and inspect CloudNativePG's recovery Job. A failed Job created before the change
can still contain the old archive configuration. Under an authorized recovery,
remove only that failed bootstrap Job after confirming the live Cluster uses the
new destination; let the operator recreate it. Preserve the Cluster, PVCs, and
backup objects.

References: [CloudNativePG recovery and archive safety](https://cloudnative-pg.io/documentation/current/appendixes/backup_barmanobjectstore/).

## Media uploads on Backblaze B2

Media uses the private `nm-authentik` bucket at the `eu-central-003` S3 endpoint.
No public bucket or custom media hostname is needed: Authentik generates temporary
signed URLs. The database archive remains in the separate `nm-authentik-pg`
bucket and keeps its existing credentials.

Create a B2 application key restricted to `nm-authentik`, with read/write access
including listing, reading, uploading, and deleting files. In Infisical's existing
production `/k3s/authentik` path, add:

| Key                             | Value                        |
| ------------------------------- | ---------------------------- |
| `backblaze-b2-media-access-key` | The media application key ID |
| `backblaze-b2-media-secret-key` | The media application key    |

Do not replace `backblaze-b2-access-key` or `backblaze-b2-secret-key`: PostgreSQL
backups and recovery use that pair. Populate the new pair before deploying the
changed media credential references, then confirm Infisical sync and pod rollout.

Configure CORS on the media bucket to allow `https://auth.nullmonkeys.com` to
perform GET and HEAD requests, with `Authorization` allowed as a request header.
Existing R2 objects must be copied to B2 with their keys and Content-Type metadata
preserved. In this version, a picker path such as `branding/logo.svg` maps to the
object `media/public/branding/logo.svg`.

Generated file URLs are cached server-side. After changing buckets, the branding
API can briefly return a signed URL for the previous bucket even after all pods
have rolled over. Allow the file URL cache to expire, or invalidate only the
affected file URL cache entries during the authorized cutover. A browser refresh
does not clear that server-side cache.

References: [Authentik S3 setup](https://docs.goauthentik.io/sys-mgmt/ops/storage-s3/),
[B2 provider configuration](https://docs.goauthentik.io/sys-mgmt/ops/storage-s3/providers/#backblaze-b2).
