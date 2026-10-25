# Cluster monitoring

`kubernetes/workloads/kube-prometheus-stack/` pins the chart and contains only
local values overrides. Its Application and chart use namespace
`kube-prometheus-stack`. Grafana is exposed at `https://grafana.nullmonkeys.com`
through Traefik, with a certificate from `letsencrypt-prod`.

## Storage and resource budget

Prometheus has one replica and a 10 GiB Longhorn volume. Metrics expire after
three days or the 6 GB retention threshold, whichever comes first. The remaining
capacity provides room for the WAL, active chunks, and compaction; retention is
not a hard filesystem quota. Scraping and rule evaluation default to 60 seconds;
cAdvisor and the private etcd job scrape every 30 seconds. Individual
ServiceMonitors and PodMonitors can override the scrape interval.
Prometheus requests 512 MiB RAM and is limited to 1 GiB.

Grafana has one replica and a 1 GiB Longhorn volume for its database and local
state. It uses a Recreate rollout so an old pod releases its ReadWriteOnce volume
before the replacement starts. It requests 128 MiB RAM and is limited to 512 MiB.
The chart supplies Kubernetes dashboards and the Prometheus datasource (UID
`prometheus`). Windows, AIX, and Darwin dashboards are not deployed. Supporting
containers have CPU/memory requests and memory limits; tune these against actual
usage, particularly Prometheus head-series growth and memory consumption.

Alertmanager has one replica and a 1 GiB Longhorn volume for notification state
and silences. It requests 64 MiB RAM and is limited to 256 MiB. Its datasource is
provisioned in Grafana; Prometheus and Alertmanager remain internal ClusterIP
services with no public ingress.

The total PVC capacity is 12 GiB. With the current Longhorn StorageClass's two
replicas, budget approximately 24 GiB of physical replica capacity, plus snapshots
and storage overhead. Longhorn is thin-provisioned; requested capacity is not
immediately written to disk. No PVC migration or live resizing is performed by
these manifests. Increasing time retention alone does not increase the existing
6 GB size threshold or resize the PVC.

## Authentication and Discord notifications

Grafana authenticates through Authentik. The Authentik application must restrict
access to `NullMonkeys`; every successful OIDC login receives Grafana's organization
Admin role. The role expression retains an extra quote layer for Grafana's INI
parser. Users must sign out and back in after changes to role mapping.

Before synchronizing these manifests, populate the following Infisical keys in
project `acbf89d5-1845-4cbe-9e2d-e623806693e8`, environment `prod`:

| Path | Key | Purpose |
| --- | --- | --- |
| `/k3s/grafana` | `client-id` | Authentik OIDC client ID |
| `/k3s/grafana` | `client-secret` | Authentik OIDC client secret |
| `/k3s/alertmanager` | `discord-webhook-url` | Discord channel webhook URL |

The two Infisical resources synchronize in wave `-1`; Argo waits for them before
deploying monitoring workloads. The Discord URL is mounted from the
`alertmanager-notifications` Secret, never stored in chart values. Secret updates
refresh hourly. Alertmanager reads the webhook URL from its mounted file.

Warnings and resolved notifications go to Discord, grouped by namespace, alert
name, and severity, with a four-hour repeat interval. Critical notifications wait
ten seconds for grouping and repeat hourly. Watchdog, InfoInhibitor, and info-only
alerts are discarded. Default inhibition rules suppress lower-severity duplicates.
No notification is sent by local validation.

Keep cluster rules in Prometheus/Alertmanager. An in-cluster Alertmanager cannot
report a total cluster outage while it is unavailable; an external availability
check can cover that separately.

## Discovery and deployment order

Longhorn (wave -9) precedes monitoring (-8); CNPG and MariaDB operator (-7), then
Authentik (-2), follow.
The existing Argo bootstrap seeds the ServiceMonitor and PodMonitor CRDs when
missing, then waits for them to become Established before creating the root
Application. This lets early services expose their own monitors without waiting
for the Prometheus workload and its Longhorn volume. The stack continues managing
all monitoring CRDs; existing clusters keep their current CRDs and ownership.

`kubernetes/bootstrap/monitoring-crds/kustomization.yaml` pins the two prerequisites
to the stack's Prometheus Operator version. Update those URLs with a chart upgrade
that changes the Operator version. Tests compare their schemas with the full
stack's CRDs. Normal bootstrap reruns skip seeding when both CRDs already exist.

Automatic pruning remains enabled: review PVC deletion and reclaim policies before
removing the monitoring Application or persistence. Prometheus discovers monitors
in all namespaces without requiring the monitoring Helm release label.

Enable discovery in the chart that owns each service:

| Service | Values setting |
| --- | --- |
| Longhorn | `metrics.serviceMonitor.enabled` |
| Traefik | `metrics.prometheus.serviceMonitor.enabled` and its metrics Service |
| cert-manager | `prometheus.servicemonitor.enabled` |
| ExternalDNS | `serviceMonitor.enabled` |
| CNPG operator | `monitoring.podMonitorEnabled` |
| MariaDB operator and webhook | `metrics.enabled` (chart ServiceMonitors enabled by default) |
| Authentik server/worker | `metrics.serviceMonitor.enabled` on each component |

Traefik's `disableAPICheck` permits offline Helm rendering; bootstrap still supplies
its real CRD. cert-manager preserves the certificate's namespace label with
`honorLabels`. Argo CD is installed from raw upstream manifests rather than a chart,
so its small ServiceMonitor is colocated at `platform/argocd/monitoring.yaml`.
CNPG creates the database PodMonitor from `Cluster.spec.monitoring.enablePodMonitor`.
The separate scheduler, controller-manager, and kube-proxy scrapes stay disabled.

The additional `kube-etcd` scrape discovers Kubernetes nodes with the control-plane
role label and uses their InternalIP on HTTP port 2381. It needs no static node-IP
list, client certificate, or public metrics endpoint. Workers are excluded.
The embedded-etcd rules and dashboard remain enabled. Traefik metrics use host
port 9101 so Node Exporter can use 9100 without a host-network port collision.

## Dashboards by ID

The stack supplies Kubernetes dashboards, and CNPG supplies its own dashboard.
The dashboard sidecar continues discovering their labelled ConfigMaps. Grafana's
separate `community` file provider downloads these dashboard IDs at startup:

| Dashboard | Grafana ID | Revision |
| --- | --- | --- |
| Traefik Official Standalone | 17346 | 9 |
| ArgoCD | 14584 | 1 |
| GitHub Actions ARC - Runner Scale Set | 25015 | 4 |
| MySQL Exporter Quickstart and Dashboard (MariaDB) | 14057 | 1 |

The small declarations live under `grafana.dashboards.community` in `values.yaml`.
All map their datasource input to UID `prometheus`. The download provider writes
under `/var/lib/grafana/dashboards/community` and displays the dashboards in
`Platform`; the ConfigMap sidecar uses `/tmp/dashboards` for the bundled dashboards.
The providers have distinct names, paths, and provisioning files.

The MariaDB operator chart does not bundle a dashboard. Dashboard 14057 is one of
the [upstream recommendations](https://github.com/mariadb-operator/mariadb-operator/blob/26.10.1/docs/metrics.md#grafana-dashboards)
and queries `mysqld_exporter` metrics directly without additional recording rules.
It monitors database instances, while the chart's `metrics.enabled` exposes only
operator and webhook metrics. Set `spec.metrics.enabled: true` on each future
`MariaDB` resource so the operator provisions its exporter and ServiceMonitor;
until then, this dashboard has no database data. Some panels depend on optional
collectors or database features and can remain empty when those are unavailable.

The ARC dashboard requires scrape job labels `arc-controller` and `arc-listeners`.
Its per-set panels also need a `runner_scale_set_name` target label, which can be
copied from the listener pod's `actions.github.com/scale-set-name` label during
target relabeling.

To add another published dashboard, append an entry under the same values key:

```yaml
grafana:
  dashboards:
    community:
      another-service:
        gnetId: 12345  # Replace with the verified dashboard ID.
        revision: 1   # Pin the revision as well as the ID.
        datasource:
          - name: DS_PROMETHEUS  # Match this dashboard's input variable.
            value: prometheus
```

Verify the dashboard's metric names, datasource input, and plugin dependencies
before choosing its revision. Pinning both ID and revision keeps the selection
repeatable; the startup download still requires access to grafana.com. Downloads
verify HTTPS certificates and fail on HTTP errors. Change a revision in Git to
update a dashboard. No dashboard JSON files are maintained in this repository.

Longhorn's published dashboards checked during this change still use the old
numeric state/robustness metrics. Longhorn 1.13 exposes state-labelled metrics, so
that dashboard import is intentionally omitted until a compatible revision is
available. Its metrics and alerts remain enabled. The bespoke overview dashboard
has also been removed; the bundled Kubernetes dashboards cover that baseline.

`allowUiUpdates: false` keeps provisioned dashboards controlled by their sources.
Manual imports and UI-only dashboards persist in Grafana's database but are not
recreated from these manifests.

## Alert rules

Keep the chart's Kubernetes, node, PVC, etcd, and Prometheus rules enabled.
`additionalPrometheusRulesMap.platform` in the stack values supplements them with
Longhorn degradation/fault/capacity,
certificate readiness/expiry, Argo application health/drift, sustained ingress
server errors, database replication lag/disconnections, stale database backups,
and absent platform targets. The chart generates a PrometheusRule with
`release: kube-prometheus-stack` because rule selection
still requires that label, unlike ServiceMonitor/PodMonitor selection.

Longhorn 1.13 robustness metrics use a `state` label and value `1` for the current
state. The backup alert uses native CNPG Barman timestamps, matching Authentik's
current database configuration. A zero timestamp means no completed base backup;
WAL archiving alone does not satisfy the alert. If backups move to the Barman
plugin, update the metric names along with that migration. The backup threshold
is 36 hours against the daily 02:00 schedule, with a 30-minute alert hold.

## Verification

Follow [development checks](development.md) to render manifests and validate the
release-matched schemas. The manifest loader handles bare `=` enum strings from
the monitoring CRDs while retaining safe YAML construction.

For read-only diagnostics on a control-plane host:

```sh
sudo k3s kubectl -n kube-prometheus-stack get pods,pvc,prometheus
sudo k3s kubectl get servicemonitors,podmonitors -A
```

Set `PROMTOOL_BIN` and `AMTOOL_BIN` when running the GitOps tests to validate custom
rule syntax, synthetic alert timing, and notification routes with
the matching Prometheus/Alertmanager release tools. The tools run locally; synthetic
Discord credentials are used only as fixtures, and no messages are sent.

After an authorized deployment, inspect Prometheus targets for the platform
services, CNPG, Authentik, Node Exporter, and every control-plane etcd endpoint.
Confirm the Traefik and Argo CD dashboards contain data and check Alertmanager's loaded
configuration. Local rendering does not verify scrape reachability, real Discord
delivery, or retention under the cluster's actual workload.

References: [chart configuration](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack),
[Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/),
[Kubernetes node discovery](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#node),
[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/),
[Discord receiver](https://prometheus.io/docs/alerting/latest/configuration/#discord_config),
[Longhorn metrics](https://longhorn.io/docs/1.13.0/monitoring/metrics/).
