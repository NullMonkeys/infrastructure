# Longhorn UI authentication

Traefik protects `https://longhorn.nullmonkeys.com` with Authentik forward auth
in single-application mode. The embedded outpost runs in the existing Authentik
server; no extra outpost deployment, client secret, or API token is required.
This gates the management UI and its proxied API; it does not add roles inside
Longhorn or change the storage data path.

## Configure Authentik

Complete these steps in the Admin interface at `https://auth.nullmonkeys.com`
before syncing the GitOps changes:

1. Under **Applications > Applications**, create an application with a provider
   (the button may be labelled **New Application** or **Create with provider**).
   Use name **Longhorn**, slug `longhorn`, and launch URL
   `https://longhorn.nullmonkeys.com`.
2. Choose **Proxy Provider**, name it `longhorn`, and select authorization flow
   `default-provider-authorization-implicit-consent`. Select
   **Forward auth (single application)** and set **External host** to
   `https://longhorn.nullmonkeys.com`. Leave unauthenticated paths empty. An
   internal host is not needed in this mode.
3. In the application's **Policy / Group / User Bindings**, bind only the group
   whose members should administer storage (for example, `NullMonkeys` if all of
   its members should have access). With no bindings, Authentik normally permits
   every authenticated user. Use your MFA-enabled authentication flow; selecting
   implicit consent alone does not enable MFA.
4. Under **Applications > Outposts**, edit **authentik Embedded Outpost**. Add
   **Longhorn** to its selected applications, preserving existing selections.
   Ensure its configuration contains
   `authentik_host: https://auth.nullmonkeys.com/`, then save.

## GitOps configuration and verification

The Longhorn ingress references a local `authentik` Middleware, which checks the
embedded outpost through `authentik-server.authentik.svc.cluster.local:80`.
The separate `longhorn-outpost` Ingress lives beside that service in the
`authentik` namespace. Its higher-priority route sends only
`/outpost.goauthentik.io/` to Authentik without the middleware, so login callbacks
do not loop. It requests its own namespace-local TLS Secret,
`longhorn-outpost-tls`, through the existing `letsencrypt-prod` issuer.
Traefik's `allowCrossNamespace: false` remains in place.

After the normal authorized GitOps rollout, confirm both the `authentik` and
`longhorn` applications have synced, then verify:

1. A private browser window visiting Longhorn redirects to Authentik.
2. An allowed group member can log in and load the UI, including volume details
   and live updates.
3. A user outside the allowed group is denied access, including direct requests
   to `https://longhorn.nullmonkeys.com/v1/`.
4. Logging out at
   `https://longhorn.nullmonkeys.com/outpost.goauthentik.io/sign_out` removes the
   application session.

If Authentik is unavailable or the provider has not been assigned to the outpost,
UI access fails instead of bypassing authentication. Authentik's database uses
Longhorn storage, so retain Kubernetes administrator access for recovery; an
authorized `kubectl -n longhorn-system port-forward svc/longhorn-frontend 8080:80`
provides local emergency UI access through Kubernetes authentication.

References: [Create a proxy provider](https://docs.goauthentik.io/add-secure-apps/providers/proxy/create-proxy-provider/),
[Traefik forward auth](https://docs.goauthentik.io/add-secure-apps/providers/proxy/server_traefik/),
[Embedded outpost](https://docs.goauthentik.io/add-secure-apps/outposts/embedded/).
