import base64
import copy
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class ManifestParserTests(unittest.TestCase):
    def test_crd_enum_equals_is_a_string_and_python_tags_are_rejected(self):
        try:
            import yaml
        except ModuleNotFoundError:
            self.skipTest("Install kubernetes/requirements.txt for manifest parsing.")

        from scripts.check_gitops import KubernetesLoader

        document = next(
            yaml.load_all("enum:\n  - =\n  - '!='\n", Loader=KubernetesLoader)
        )
        self.assertEqual(document["enum"], ["=", "!="])
        with self.assertRaises(yaml.constructor.ConstructorError):
            yaml.load("!!python/tuple [unsafe]", Loader=KubernetesLoader)


@unittest.skipUnless(
    os.environ.get("KUBECTL_BIN"),
    "Set KUBECTL_BIN and install kubernetes/requirements.txt for GitOps checks.",
)
class GitOpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from scripts import check_gitops

        cls.check = check_gitops
        cls.bundles = check_gitops.build_manifests(
            ROOT, os.environ["KUBECTL_BIN"], os.environ.get("HELM_BIN", "helm")
        )

    def test_all_local_sources_render_and_match_their_projects(self):
        self.check.validate_references(self.bundles)

    def test_all_applications_read_the_public_repository_over_https(self):
        repository_url = "https://github.com/NullMonkeys/infrastructure.git"
        documents = [
            *self.bundles["kubernetes/bootstrap"],
            *self.bundles["kubernetes/bootstrap/applications"],
        ]
        for document in documents:
            with self.subTest(kind=document["kind"], name=document["metadata"]["name"]):
                if document["kind"] == "Application":
                    self.assertEqual(document["spec"]["source"]["repoURL"], repository_url)
                elif document["kind"] == "AppProject":
                    self.assertEqual(document["spec"]["sourceRepos"], [repository_url])

    def test_root_and_services_share_one_project_across_namespaces(self):
        projects = [
            doc
            for doc in self.bundles["kubernetes/bootstrap"]
            if doc["kind"] == "AppProject"
        ]
        self.assertEqual(len(projects), 1)
        project_name = projects[0]["metadata"]["name"]
        applications = [
            doc
            for doc in self.bundles["kubernetes/bootstrap"]
            if doc["kind"] == "Application"
        ]
        applications += self.bundles["kubernetes/bootstrap/applications"]
        self.assertEqual(
            {doc["spec"]["project"] for doc in applications}, {project_name}
        )
        bundles = copy.deepcopy(self.bundles)
        bundles["kubernetes/bootstrap/applications"][0]["spec"]["destination"][
            "namespace"
        ] = "future-workload"
        self.check.validate_references(bundles)

    def test_missing_source_and_unauthorized_destinations_are_rejected(self):
        for mutation in (
            lambda spec: spec["source"].update(path="kubernetes/apps/missing"),
            lambda spec: spec["destination"].update(
                server="https://other-cluster.invalid"
            ),
            lambda spec: spec["source"].update(
                repoURL="https://github.com/other/repository.git"
            ),
        ):
            bundles = copy.deepcopy(self.bundles)
            mutation(bundles["kubernetes/bootstrap/applications"][0]["spec"])
            with self.assertRaises(ValueError):
                self.check.validate_references(bundles)

    def test_root_cannot_reconcile_its_own_bootstrap(self):
        bundles = copy.deepcopy(self.bundles)
        root = next(
            doc
            for doc in bundles["kubernetes/bootstrap"]
            if doc["kind"] == "Application"
        )
        root["spec"]["source"]["path"] = "kubernetes/bootstrap"
        with self.assertRaises(ValueError):
            self.check.validate_references(bundles)

    def test_application_schema_comes_from_the_rendered_crds(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.check.export_crd_schemas(
                self.bundles["kubernetes/platform/argocd"], Path(temporary)
            )
            schema = json.loads(
                (Path(temporary) / "argoproj.io/application_v1alpha1.json").read_text()
            )
            self.assertEqual(schema["properties"]["kind"]["enum"], ["Application"])
            self.assertEqual(
                schema["properties"]["apiVersion"]["enum"], ["argoproj.io/v1alpha1"]
            )
            self.assertFalse(schema["additionalProperties"])

    def test_strict_schemas_reject_unknown_fields_but_preserve_free_form_values(self):
        schema = {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "properties": {"project": {"type": "string"}},
                },
                "values": {
                    "type": "object",
                    "x-kubernetes-preserve-unknown-fields": True,
                    "properties": {},
                },
                "labels": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
        }
        self.check.make_schema_strict(schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["properties"]["spec"]["additionalProperties"])
        self.assertNotIn("additionalProperties", schema["properties"]["values"])
        self.assertEqual(
            schema["properties"]["labels"]["additionalProperties"], {"type": "string"}
        )

    def test_self_management_keeps_the_server_executable_and_tls_at_ingress(self):
        documents = self.bundles["kubernetes/platform/argocd"]
        server = next(
            doc
            for doc in documents
            if doc["kind"] == "Deployment"
            and doc["metadata"]["name"] == "argocd-server"
        )
        container = server["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(
            container["args"], ["/usr/local/bin/argocd-server", "--insecure"]
        )
        certificate = next(doc for doc in documents if doc["kind"] == "Certificate")
        ingress = next(doc for doc in documents if doc["kind"] == "IngressRoute")
        self.assertEqual(
            certificate["spec"]["secretName"], ingress["spec"]["tls"]["secretName"]
        )
        for hostname in certificate["spec"]["dnsNames"]:
            self.assertTrue(
                all(
                    f"Host(`{hostname}`)" in route["match"]
                    for route in ingress["spec"]["routes"]
                )
            )
        grpc = next(
            route
            for route in ingress["spec"]["routes"]
            if "application/grpc" in route["match"]
        )
        self.assertEqual(grpc["services"][0]["scheme"], "h2c")
        self.assertNotIn("certResolver", ingress["spec"]["tls"])
        self.assertEqual(ingress["spec"]["ingressClassName"], "traefik")
        bootstrap_config = next(
            doc
            for doc in self.bundles["kubernetes/bootstrap"]
            if doc["kind"] == "ConfigMap"
        )
        self.assertEqual(
            bootstrap_config["data"], {"kustomize.buildOptions": "--enable-helm"}
        )
        service_config = next(
            doc
            for doc in documents
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "argocd-cm"
        )
        self.assertNotIn("kustomize.buildOptions", service_config["data"])
        application = next(
            doc
            for doc in self.bundles["kubernetes/bootstrap/applications"]
            if doc["metadata"]["name"] == "argocd"
        )
        self.assertIn(
            "/data/kustomize.buildOptions",
            application["spec"]["ignoreDifferences"][0]["jsonPointers"],
        )
        self.assertIn(
            "RespectIgnoreDifferences=true",
            application["spec"]["syncPolicy"]["syncOptions"],
        )

    def test_dns_challenge_uses_the_secret_synced_for_cert_manager(self):
        issuer = next(
            doc
            for doc in self.bundles["kubernetes/platform/cert-manager"]
            if doc["kind"] == "ClusterIssuer"
        )
        solver = issuer["spec"]["acme"]["solvers"][0]
        self.assertNotIn("http01", solver)
        self.assertEqual(solver["selector"]["dnsZones"], ["nullmonkeys.com"])
        reference = solver["dns01"]["cloudflare"]["apiTokenSecretRef"]
        sync = next(
            doc
            for doc in self.bundles["kubernetes/platform/cert-manager"]
            if doc["kind"] == "InfisicalStaticSecret"
        )
        target = sync["spec"]["targets"][0]
        self.assertEqual(target["name"], reference["name"])
        self.assertEqual(target["namespace"], "cert-manager")
        self.assertEqual(target["kind"], "Secret")
        self.assertEqual(reference["key"], "cloudflare-api-token")
        self.assertEqual(sync["spec"]["sources"][0]["secretPath"], "/k3s/cert-manager")
        certificate = next(
            doc
            for doc in self.bundles["kubernetes/platform/argocd"]
            if doc["kind"] == "Certificate"
        )
        self.assertEqual(
            certificate["spec"]["issuerRef"]["name"], issuer["metadata"]["name"]
        )
        applications = {
            doc["metadata"]["name"]: int(
                doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]
            )
            for doc in self.bundles["kubernetes/bootstrap/applications"]
        }
        self.assertLess(applications["infisical"], applications["cert-manager"])
        self.assertLess(applications["cert-manager"], applications["argocd"])
        self.assertLess(
            int(sync["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
            int(issuer["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
        )

    def test_external_dns_follows_ready_traefik_pods_and_scopes_ownership(self):
        documents = self.bundles["kubernetes/platform/external-dns"]
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        arguments = container["args"]
        for argument in (
            "--provider=cloudflare",
            "--source=service",
            "--source=ingress",
            "--source=traefik-proxy",
            "--domain-filter=nullmonkeys.com",
            "--zone-id-filter=$(CF_ZONE_ID)",
            "--registry=txt",
            "--txt-owner-id=nullmonkeys-k3s",
            "--txt-prefix=external-dns-",
            "--policy=sync",
        ):
            self.assertIn(argument, arguments)
        self.assertNotIn("--publish-host-ip", arguments)
        self.assertIn("--cloudflare-proxied", arguments)
        # v0.23's boolean parser rejects explicit '=true'/'=false' values.
        self.assertFalse(
            any(arg.startswith("--cloudflare-proxied=") for arg in arguments)
        )
        self.assertFalse(
            any(arg.startswith("--service-type-filter=") for arg in arguments)
        )
        self.assertNotIn("--managed-record-types=AAAA", arguments)
        environment = {entry["name"]: entry for entry in container["env"]}
        sync = next(
            doc for doc in documents if doc["kind"] == "InfisicalStaticSecret"
        )
        target = sync["spec"]["targets"][0]
        self.assertEqual(sync["spec"]["sources"][0]["secretPath"], "/k3s/external-dns")
        for variable, key in (
            ("CF_API_TOKEN", "cloudflare-api-token"),
            ("CF_ZONE_ID", "cloudflare-zone-id"),
        ):
            reference = environment[variable]["valueFrom"]["secretKeyRef"]
            self.assertEqual(reference, {"name": target["name"], "key": key})
        self.assertEqual(target["namespace"], deployment["metadata"]["namespace"])
        rules = next(doc for doc in documents if doc["kind"] == "ClusterRole")["rules"]
        for resource in (
            "nodes", "pods", "services", "endpointslices", "ingressroutes", "ingresses"
        ):
            # Informers list/watch nodes; lookups use their local cache.
            self.assertTrue(
                any(
                    resource in rule["resources"]
                    and {"list", "watch"} <= set(rule["verbs"])
                    for rule in rules
                ),
                resource,
            )
        self.assertTrue(
            all(set(rule["verbs"]) <= {"get", "list", "watch"} for rule in rules)
        )

        traefik = self.bundles["kubernetes/platform/traefik"]
        service = next(
            doc for doc in traefik
            if doc["kind"] == "Service" and doc["metadata"]["name"] == "traefik-dns"
        )
        pod = next(doc for doc in traefik if doc["kind"] == "Deployment")["spec"][
            "template"
        ]
        self.assertEqual(service["spec"]["clusterIP"], "None")
        self.assertFalse(service["spec"]["publishNotReadyAddresses"])
        self.assertTrue(
            service["spec"]["selector"].items() <= pod["metadata"]["labels"].items()
        )
        annotations = service["metadata"]["annotations"]
        self.assertEqual(
            annotations["external-dns.kubernetes.io/endpoints-type"], "NodeExternalIP"
        )
        hostname = annotations["external-dns.kubernetes.io/hostname"]
        traefik_arguments = pod["spec"]["containers"][0]["args"]
        self.assertIn(
            f"--providers.kubernetesingress.ingressendpoint.hostname={hostname}",
            traefik_arguments,
        )
        routes = [
            doc for bundle in self.bundles.values() for doc in bundle
            if doc["kind"] in ("Ingress", "IngressRoute")
        ]
        self.assertEqual(len(routes), 4)
        for route in routes:
            self.assertEqual(
                route["metadata"]["annotations"]["external-dns.kubernetes.io/target"],
                hostname,
            )

    def test_longhorn_checker_follows_prerequisites_and_precedes_workloads(self):
        documents = self.bundles["kubernetes/platform/longhorn"]
        checker = next(
            doc for doc in documents
            if doc["kind"] == "Job"
            and doc["metadata"]["name"] == "longhorn-pre-upgrade"
        )
        annotations = checker["metadata"]["annotations"]
        self.assertEqual(annotations["argocd.argoproj.io/hook"], "Sync")
        self.assertNotIn("helm.sh/hook", annotations)
        self.assertEqual(
            set(annotations["argocd.argoproj.io/hook-delete-policy"].split(",")),
            {"BeforeHookCreation", "HookSucceeded"},
        )
        checker_wave = int(annotations["argocd.argoproj.io/sync-wave"])
        service_account = checker["spec"]["template"]["spec"]["serviceAccountName"]
        self.assertTrue(
            any(
                doc["kind"] == "ServiceAccount"
                and doc["metadata"]["name"] == service_account
                for doc in documents
            )
        )
        bindings = [
            doc for doc in documents if doc["kind"] == "ClusterRoleBinding"
            and any(
                subject.get("name") == service_account for subject in doc["subjects"]
            )
        ]
        self.assertTrue(bindings)
        for binding in bindings:
            self.assertTrue(
                any(
                    doc["kind"] == binding["roleRef"]["kind"]
                    and doc["metadata"]["name"] == binding["roleRef"]["name"]
                    for doc in documents
                )
            )
        for doc in documents:
            annotations = doc["metadata"].get("annotations", {})
            wave = int(annotations.get("argocd.argoproj.io/sync-wave", "0"))
            if doc["kind"] in {
                "CustomResourceDefinition", "ServiceAccount", "ClusterRole",
                "ClusterRoleBinding", "Role", "RoleBinding",
            }:
                self.assertLess(wave, checker_wave)
                self.assertNotIn("argocd.argoproj.io/hook", annotations)
            elif doc["kind"] in {"Deployment", "DaemonSet", "StatefulSet"}:
                self.assertGreater(wave, checker_wave)

    def test_authentik_recovery_keeps_source_and_destination_archives_separate(self):
        cluster = next(
            doc for doc in self.bundles["kubernetes/workloads/authentik"]
            if doc["kind"] == "Cluster"
        )
        spec = cluster["spec"]
        recovery = spec["bootstrap"]["recovery"]
        source = next(
            source["barmanObjectStore"] for source in spec["externalClusters"]
            if source["name"] == recovery["source"]
        )
        destination = spec["backup"]["barmanObjectStore"]
        self.assertEqual(source["serverName"], "authentik-db")
        self.assertNotEqual(
            (source["endpointURL"], source["destinationPath"], source["serverName"]),
            (destination["endpointURL"], destination["destinationPath"], destination["serverName"]),
        )
        self.assertNotIn(
            "cnpg.io/skipEmptyWalArchiveCheck",
            cluster["metadata"].get("annotations", {}),
        )

    def test_authentik_media_credentials_are_separate_from_database_backups(self):
        documents = self.bundles["kubernetes/workloads/authentik"]
        cluster = next(doc for doc in documents if doc["kind"] == "Cluster")
        database_keys = {
            (reference["name"], reference["key"])
            for reference in cluster["spec"]["backup"]["barmanObjectStore"][
                "s3Credentials"
            ].values()
        }
        for deployment in (doc for doc in documents if doc["kind"] == "Deployment"):
            for container in deployment["spec"]["template"]["spec"]["containers"]:
                env = {entry["name"]: entry for entry in container.get("env", [])}
                if "AUTHENTIK_STORAGE__BACKEND" not in env:
                    continue
                self.assertEqual(
                    env["AUTHENTIK_STORAGE__S3__REGION"]["value"], "eu-central-003"
                )
                media_keys = {
                    (env[key]["valueFrom"]["secretKeyRef"]["name"],
                     env[key]["valueFrom"]["secretKeyRef"]["key"])
                    for key in (
                        "AUTHENTIK_STORAGE__S3__ACCESS_KEY",
                        "AUTHENTIK_STORAGE__S3__SECRET_KEY",
                    )
                }
                self.assertTrue(media_keys.isdisjoint(database_keys))

    def test_traefik_trusts_only_cloudflare_for_forwarded_headers(self):
        documents = self.bundles["kubernetes/platform/traefik"]
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        self.assertTrue(pod["hostNetwork"])
        self.assertEqual(pod["dnsPolicy"], "ClusterFirstWithHostNet")
        self.assertEqual(deployment["spec"]["strategy"]["rollingUpdate"]["maxSurge"], 0)
        self.assertTrue(
            pod["affinity"]["podAntiAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]
        )
        container = pod["containers"][0]
        self.assertEqual(container["securityContext"]["runAsUser"], 0)
        self.assertFalse(container["securityContext"]["runAsNonRoot"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertIn(
            "NET_BIND_SERVICE", container["securityContext"]["capabilities"]["add"]
        )
        arguments = container["args"]
        for argument in (
            "--entryPoints.web.address=:80/tcp",
            "--entryPoints.websecure.address=:443/tcp",
        ):
            self.assertIn(argument, arguments)
        trusted = [
            argument
            for argument in arguments
            if "forwardedheaders.trustedips=" in argument.lower()
        ]
        self.assertEqual(len(trusted), 2)
        for argument in trusted:
            networks = [
                ipaddress.ip_network(value)
                for value in argument.split("=", 1)[1].split(",")
            ]
            self.assertTrue(
                all(
                    network.version == 4 and network.prefixlen > 0
                    for network in networks
                )
            )
            self.assertIn(ipaddress.ip_network("173.245.48.0/20"), networks)
        self.assertFalse(
            any(
                "insecure=true" in argument.lower()
                or "proxyprotocol" in argument.lower()
                for argument in arguments
            )
        )
        services = [doc for doc in documents if doc["kind"] == "Service"]
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["spec"]["clusterIP"], "None")

    def test_infisical_health_waits_for_the_current_generation_and_successful_sync(
        self,
    ):
        lua = os.environ.get("LUA_BIN")
        if not lua:
            self.skipTest("Set LUA_BIN to execute the Argo health check locally.")
        config = next(
            doc
            for doc in self.bundles["kubernetes/platform/argocd"]
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "argocd-cm"
        )
        health = config["data"][
            "resource.customizations.health.secrets.infisical.com_InfisicalStaticSecret"
        ]
        cases = (
            ("{metadata={generation=2}}", "Progressing"),
            (
                "{metadata={generation=2},status={observedGeneration=1,error=false}}",
                "Progressing",
            ),
            (
                "{metadata={generation=2},status={observedGeneration=2,error=true}}",
                "Degraded",
            ),
            (
                "{metadata={generation=2},status={observedGeneration=2,error=false,conditions={}}}",
                "Progressing",
            ),
            (
                '{metadata={generation=2},status={observedGeneration=2,error=false,conditions={{type="secrets.infisical.com/LastReconcileStatus",status="True"}}}}',
                "Healthy",
            ),
        )
        for obj, expected in cases:
            with self.subTest(expected=expected, obj=obj):
                script = f'local obj = {obj}\nlocal function health()\n{health}\nend\nassert(health().status == "{expected}")'
                subprocess.run([lua, "-"], input=script, text=True, check=True)

    def test_each_service_has_one_application_pointing_to_its_kustomization(self):
        applications = self.bundles["kubernetes/bootstrap/applications"]
        service_paths = {
            path
            for path in self.bundles
            if Path(path).parent
            in {Path("kubernetes/platform"), Path("kubernetes/workloads")}
        }
        self.assertEqual(
            {doc["spec"]["source"]["path"] for doc in applications}, service_paths
        )
        self.assertEqual(len(applications), len(service_paths))
        for application in applications:
            self.assertNotIn("sources", application["spec"])
            source = application["spec"]["source"]
            self.assertNotIn("helm", source)
            self.assertIn(source["path"], self.bundles)

    def test_monitoring_storage_dependencies_and_cross_namespace_discovery(self):
        documents = self.bundles["kubernetes/workloads/kube-prometheus-stack"]
        prometheus = next(doc for doc in documents if doc["kind"] == "Prometheus")
        spec = prometheus["spec"]
        self.assertEqual(spec["replicas"], 1)
        self.assertEqual(spec["retention"], "3d")
        self.assertEqual(spec["retentionSize"], "6GB")
        storage = spec["storage"]["volumeClaimTemplate"]["spec"]
        self.assertEqual(storage["storageClassName"], "longhorn")
        self.assertEqual(storage["resources"]["requests"]["storage"], "10Gi")
        grafana = next(
            doc for doc in documents if doc["kind"] == "PersistentVolumeClaim"
        )
        self.assertEqual(grafana["spec"]["storageClassName"], "longhorn")
        self.assertEqual(grafana["spec"]["resources"]["requests"]["storage"], "1Gi")
        for selector in (
            "serviceMonitorSelector",
            "podMonitorSelector",
            "serviceMonitorNamespaceSelector",
            "podMonitorNamespaceSelector",
        ):
            self.assertEqual(spec[selector], {})
        applications = {
            doc["metadata"]["name"]: doc
            for doc in self.bundles["kubernetes/bootstrap/applications"]
        }
        waves = {
            name: int(doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
            for name, doc in applications.items()
        }
        self.assertLess(waves["longhorn"], waves["kube-prometheus-stack"])
        self.assertLess(waves["kube-prometheus-stack"], waves["cloudnative-pg"])
        self.assertLess(waves["cloudnative-pg"], waves["authentik"])
        self.assertEqual(
            applications["kube-prometheus-stack"]["spec"]["destination"]["namespace"],
            prometheus["metadata"]["namespace"],
        )

    def test_monitoring_host_ports_and_etcd_node_discovery(self):
        import yaml

        documents = self.bundles["kubernetes/workloads/kube-prometheus-stack"]
        exporter = next(doc for doc in documents if doc["kind"] == "DaemonSet")
        traefik = next(
            doc
            for doc in self.bundles["kubernetes/platform/traefik"]
            if doc["kind"] == "Deployment"
        )
        exporter_pod = exporter["spec"]["template"]["spec"]
        traefik_pod = traefik["spec"]["template"]["spec"]
        self.assertTrue(exporter_pod["hostNetwork"])
        self.assertTrue(traefik_pod["hostNetwork"])
        exporter_ports = {
            port["containerPort"]
            for c in exporter_pod["containers"]
            for port in c.get("ports", [])
        }
        traefik_ports = {
            port["containerPort"]
            for c in traefik_pod["containers"]
            for port in c.get("ports", [])
        }
        self.assertTrue(exporter_ports.isdisjoint(traefik_ports))
        prometheus = next(doc for doc in documents if doc["kind"] == "Prometheus")
        reference = prometheus["spec"]["additionalScrapeConfigs"]
        secret = next(
            doc
            for doc in documents
            if doc["kind"] == "Secret" and doc["metadata"]["name"] == reference["name"]
        )
        job = yaml.safe_load(base64.b64decode(secret["data"][reference["key"]]))[0]
        self.assertEqual(job["job_name"], "kube-etcd")
        self.assertEqual(job["scheme"], "http")
        self.assertEqual(job["kubernetes_sd_configs"], [{"role": "node"}])
        # Exercise the rendered Prometheus relabel rules with a server and a worker.
        for control_plane in (True, False):
            labels = {
                "__meta_kubernetes_node_address_InternalIP": "10.10.1.10",
                "__meta_kubernetes_node_address_ExternalIP": "192.0.2.10",
                "__meta_kubernetes_node_name": "example-node",
            }
            if control_plane:
                labels[
                    "__meta_kubernetes_node_labelpresent_node_role_kubernetes_io_control_plane"
                ] = "true"
            kept = True
            for rule in job["relabel_configs"]:
                value = ";".join(labels.get(key, "") for key in rule["source_labels"])
                match = re.fullmatch(rule.get("regex", "(.*)"), value)
                if rule.get("action", "replace") == "keep":
                    if not match:
                        kept = False
                        break
                elif match:
                    labels[rule["target_label"]] = rule.get(
                        "replacement", "$1"
                    ).replace("$1", match.group(1))
            self.assertEqual(kept, control_plane)
            if kept:
                self.assertEqual(labels["__address__"], "10.10.1.10:2381")
                self.assertEqual(labels["instance"], "example-node")
        self.assertFalse(
            any(
                doc["kind"] == "Service"
                and doc["metadata"]["name"].endswith("-kube-etcd")
                for doc in documents
            )
        )

    def test_build_discards_stale_chart_caches_without_changing_the_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ansible/vars").mkdir(parents=True)
            (root / "ansible/vars/k3s.yml").write_text("k3s_version: v1.36.5+k3s1\n")
            service = root / "kubernetes/example"
            cache = service / "charts/stale"
            cache.mkdir(parents=True)
            (cache / "Chart.yaml").write_text("version: stale")
            (service / "kustomization.yaml").write_text("kind: Kustomization\n")

            def render(command, **kwargs):
                copied_service = Path(command[2])
                self.assertNotEqual(copied_service, service)
                self.assertFalse((copied_service / "charts").exists())
                self.assertIn("--enable-helm", command)
                return subprocess.CompletedProcess(
                    command, 0, stdout="kind: ConfigMap\n"
                )

            with patch.object(self.check.subprocess, "run", side_effect=render):
                self.check.build_manifests(root, "kubectl", "helm")
            self.assertEqual((cache / "Chart.yaml").read_text(), "version: stale")

    def test_exporting_schemas_does_not_modify_the_crds_being_validated(self):
        documents = self.bundles["kubernetes/platform/argocd"]
        before = copy.deepcopy(documents)
        with tempfile.TemporaryDirectory() as directory:
            self.check.export_crd_schemas(documents, Path(directory))
        self.assertEqual(documents, before)


if __name__ == "__main__":
    unittest.main()
