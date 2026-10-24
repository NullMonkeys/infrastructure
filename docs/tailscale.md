# Tailscale Kubernetes Operator

Argo CD manages the `tailscale-operator` Application in namespace `tailscale`,
using the stable Helm chart pinned in `kubernetes/platform/tailscale/kustomization.yaml`.
This installs the operator, its CRDs, RBAC, and the non-default `tailscale`
IngressClass. The operator joins the tailnet as `tailscale-operator`, tagged
`tag:k8s-operator`. API server proxying and impersonation remain disabled.
No workloads, routes, or Kubernetes API access are exposed by this configuration.

## Tailnet prerequisites

Before the authorized GitOps rollout:

1. In the Tailscale admin console's **Access controls**, merge these entries into
   the existing `tagOwners` object, preserving the rest of the policy:

   ```json
   "tag:k8s-operator": [],
   "tag:k8s": ["tag:k8s-operator"]
   ```

2. Under **Trust credentials**, create an OAuth client named `k3s-operator`.
   Grant **write** permissions for **General/Services**, **Devices/Core**, and
   **Keys/Auth Keys**, scoped to `tag:k8s-operator`.
3. In Infisical project `acbf89d5-1845-4cbe-9e2d-e623806693e8`, environment
   `prod`, populate `/k3s/tailscale` with `client-id` and `client-secret`.
   Give the existing cluster machine identity read access to this path.

The tag ownership lets the operator manage future devices tagged `tag:k8s`;
it does not add traffic-access rules. Service exposure and routing are deferred.

## Credential sync and startup

The consumer's `InfisicalStaticSecret` uses the existing `cluster-secrets`
authentication reference and copies `/k3s/tailscale` into Secret
`tailscale/operator-oauth` every hour. Keys retain their hyphenated names.
The chart's `oauthSecretVolume` maps `client-id` to `/oauth/client_id` and
`client-secret` to `/oauth/client_secret`. Helm receives no credential values.

Infisical starts at Application wave `-10`, followed by Tailscale at `-9`.
Inside the Tailscale Application, the secret sync runs at resource wave `-1`;
the existing Infisical health check waits for successful reconciliation before
the chart's resources at wave `0` are applied. A failed Infisical sync blocks
the chart resources. Missing `client-id` or `client-secret` keys prevent the
operator Pod from mounting its credentials and starting.
`creationPolicy: Owner` means deleting the sync also deletes its generated Secret.

## Verify after rollout

After an authorized deployment, inspect status without printing credential values:

```sh
kubectl -n argocd get application tailscale-operator
kubectl -n tailscale get infisicalstaticsecret tailscale-oauth-sync
kubectl -n tailscale get secret operator-oauth
kubectl -n tailscale rollout status deployment/operator --timeout=180s
kubectl -n tailscale get pods
kubectl get ingressclass tailscale
kubectl get crds
```

Confirm the Application is **Synced/Healthy**, the secret sync reports a successful
reconciliation, and the operator Deployment is available. In the Tailscale admin
console's **Machines** page, confirm `tailscale-operator` appears with
`tag:k8s-operator`. If it does not register, check the OAuth client's scopes and
tag, the tag ownership entries, and the Infisical path/read permissions.
Do not print Secret YAML or credential values during troubleshooting.

Local rendering and schema checks do not establish live registration or connectivity.

References: [Operator installation](https://tailscale.com/docs/kubernetes-operator/install-operator),
[pinned chart values](https://github.com/tailscale/tailscale/blob/v1.104.2/cmd/k8s-operator/deploy/chart/values.yaml).
