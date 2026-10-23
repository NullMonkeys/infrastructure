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
individual ServiceMonitors and PodMonitors can override the scrape interval.
Prometheus requests 512 MiB RAM and is limited to 1 GiB.

Grafana has one replica and a 1 GiB Longhorn volume for its database and local
state. It uses a Recreate rollout so an old pod releases its ReadWriteOnce volume
before the replacement starts. It requests 128 MiB RAM and is limited to 512 MiB.
The chart supplies dashboards and the Prometheus datasource. Alertmanager and
its Grafana datasource are disabled; no Alertmanager notifications are delivered.

The total PVC capacity is 11 GiB. With the current Longhorn StorageClass's two
replicas, budget approximately 22 GiB of physical replica capacity, plus snapshots
and storage overhead. Longhorn is thin-provisioned; requested capacity is not
immediately written to disk. No PVC migration or live resizing is performed by
these manifests. Grafana's admin credential remains chart-managed until an
Infisical credential or SSO configuration is added.

## Discovery and deployment order

Longhorn (wave -9) precedes monitoring (-8); CNPG (-7) and Authentik (0) follow.
Keep the monitoring CRDs available before any dependent service creates a
ServiceMonitor or PodMonitor. Automatic pruning is enabled: review PVC deletion
and reclaim policies before removing the monitoring Application or persistence.

Prometheus discovers ServiceMonitors and PodMonitors in all namespaces without
requiring the monitoring Helm release label. The separate scheduler,
controller-manager, and kube-proxy scrapes are disabled for K3s.

The additional `kube-etcd` scrape discovers Kubernetes nodes with the control-plane
role label and uses their InternalIP on HTTP port 2381. It needs no static node-IP
list, client certificate, or public metrics endpoint. Workers are excluded.
The embedded-etcd rules and dashboard remain enabled. Traefik metrics use host
port 9101 so Node Exporter can use 9100 without a host-network port collision.

## Verification

Follow [development checks](development.md) to render manifests and validate the
release-matched schemas. The manifest loader handles bare `=` enum strings from
the monitoring CRDs while retaining safe YAML construction.

For read-only diagnostics on a control-plane host:

```sh
sudo k3s kubectl -n kube-prometheus-stack get pods,pvc,prometheus
sudo k3s kubectl get servicemonitors,podmonitors -A
```

Inspect Prometheus targets after deployment to verify CNPG, Authentik, Node
Exporter, and every control-plane node's etcd endpoint. Local rendering does not
verify scrape reachability or retention under the cluster's actual workload.

References: [chart configuration](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack),
[Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/),
[Kubernetes node discovery](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#node).
