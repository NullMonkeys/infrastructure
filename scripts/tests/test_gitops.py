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

    def test_argocd_oidc_uses_synced_credentials_and_waits_for_authentik(self):
        import yaml

        documents = self.bundles["kubernetes/platform/argocd"]
        config = next(
            doc for doc in documents
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "argocd-cm"
        )
        self.assertEqual(config["data"]["users.anonymous.enabled"], "false")
        connector = yaml.safe_load(config["data"]["dex.config"])["connectors"][0]
        self.assertEqual(connector["type"], "oidc")
        oidc = connector["config"]
        self.assertEqual(
            oidc["issuer"], "https://auth.nullmonkeys.com/application/o/argocd/"
        )
        self.assertTrue(oidc["insecureEnableGroups"])
        self.assertEqual(set(oidc["scopes"]), {"openid", "profile", "email"})
        server = next(
            doc for doc in documents
            if doc["kind"] == "Deployment"
            and doc["metadata"]["name"] == "argocd-server"
        )
        self.assertEqual(
            server["spec"]["template"]["metadata"]["annotations"][
                "nullmonkeys.com/sso-provider"
            ],
            connector["id"],
        )
        sync = next(doc for doc in documents if doc["kind"] == "InfisicalStaticSecret")
        target = sync["spec"]["targets"][0]
        for field, key in (("clientID", "client-id"), ("clientSecret", "client-secret")):
            self.assertEqual(oidc[field], f"${target['name']}:{key}")
        self.assertNotEqual(target["name"], "argocd-secret")
        self.assertEqual(target["namespace"], config["metadata"]["namespace"])
        self.assertEqual(target["kind"], "Secret")
        self.assertEqual(target["creationPolicy"], "Owner")
        self.assertEqual(
            target["metadata"]["labels"]["app.kubernetes.io/part-of"], "argocd"
        )
        self.assertEqual(sync["spec"]["sources"][0]["secretPath"], "/k3s/argocd")
        sync_wave = int(sync["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"])
        config_wave = int(config["metadata"].get("annotations", {}).get(
            "argocd.argoproj.io/sync-wave", "0"
        ))
        self.assertLess(sync_wave, config_wave)
        self.assertLess(config_wave, int(server["metadata"]["annotations"][
            "argocd.argoproj.io/sync-wave"
        ]))
        rbac = next(
            doc for doc in documents
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "argocd-rbac-cm"
        )["data"]
        self.assertEqual(rbac["policy.default"], "")
        self.assertEqual(rbac["policy.csv"].strip(), "g, NullMonkeys, role:admin")
        self.assertEqual(yaml.safe_load(rbac["scopes"]), ["groups"])
        applications = {
            doc["metadata"]["name"]: int(
                doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]
            )
            for doc in self.bundles["kubernetes/bootstrap/applications"]
        }
        self.assertLess(applications["infisical"], applications["authentik"])
        self.assertLess(applications["cloudnative-pg"], applications["authentik"])
        self.assertLess(applications["authentik"], applications["argocd"])

    def test_argocd_rbac_grants_admin_only_to_nullmonkeys(self):
        import yaml

        argocd = os.environ.get("ARGOCD_BIN")
        if not argocd:
            self.skipTest("Set ARGOCD_BIN to check SSO permissions with Argo's RBAC engine.")
        rbac = next(
            doc for doc in self.bundles["kubernetes/platform/argocd"]
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "argocd-rbac-cm"
        )
        with tempfile.TemporaryDirectory() as temporary:
            policy = Path(temporary) / "argocd-rbac-cm.yaml"
            policy.write_text(yaml.safe_dump(rbac))
            command = [argocd, "admin", "settings", "rbac"]
            subprocess.run(
                [*command, "validate", "--policy-file", str(policy)],
                check=True, capture_output=True, text=True,
            )
            for subject, expected in (("NullMonkeys", "Yes"), ("outsider", "No")):
                for action in ("get", "sync", "delete"):
                    with self.subTest(subject=subject, action=action):
                        result = subprocess.run(
                            [*command, "can", subject, action, "applications",
                             "infrastructure/example", "--policy-file", str(policy)],
                            capture_output=True, text=True,
                        )
                        self.assertEqual(result.stdout.strip(), expected, result.stderr)
                        self.assertEqual(result.returncode, 0 if expected == "Yes" else 1)

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

    def test_tailscale_uses_infisical_credentials_before_operator_startup(self):
        documents = self.bundles["kubernetes/platform/tailscale"]
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        container = next(item for item in pod["containers"] if item["name"] == "operator")
        sync = next(doc for doc in documents if doc["kind"] == "InfisicalStaticSecret")
        target = sync["spec"]["targets"][0]
        self.assertEqual(sync["spec"]["infisicalAuthRef"], {
            "name": "cluster-secrets", "namespace": "infisical-operator-system",
        })
        self.assertEqual(sync["spec"]["sources"][0]["secretPath"], "/k3s/tailscale")
        self.assertEqual(sync["spec"]["sources"][0]["environmentSlug"], "prod")
        self.assertEqual(sync["spec"]["syncOptions"]["refreshInterval"], "1h")
        self.assertEqual(target["namespace"], deployment["metadata"]["namespace"])
        self.assertEqual(target["kind"], "Secret")
        self.assertEqual(target["creationPolicy"], "Owner")
        volume = next(item for item in pod["volumes"] if item["name"] == "oauth")
        self.assertEqual(volume["secret"]["secretName"], target["name"])
        self.assertFalse(any(
            doc["kind"] == "Secret" and doc["metadata"]["name"] == target["name"]
            for doc in documents
        ), "Only Infisical should own the OAuth Secret")
        mount = next(item for item in container["volumeMounts"] if item["name"] == "oauth")
        self.assertTrue(mount["readOnly"])
        environment = {entry["name"]: entry.get("value") for entry in container["env"]}
        paths = {item["key"]: item["path"] for item in volume["secret"]["items"]}
        for key, variable in (
            ("client-id", "CLIENT_ID_FILE"),
            ("client-secret", "CLIENT_SECRET_FILE"),
        ):
            self.assertEqual(environment[variable], f"{mount['mountPath']}/{paths[key]}")
        self.assertNotIn("CLIENT_SECRET", environment)

        def wave(document):
            return int(document["metadata"].get("annotations", {}).get(
                "argocd.argoproj.io/sync-wave", "0"
            ))

        self.assertLess(wave(sync), wave(deployment))
        applications = {
            doc["metadata"]["name"]: doc
            for doc in self.bundles["kubernetes/bootstrap/applications"]
        }
        self.assertLess(wave(applications["infisical"]), wave(applications["tailscale-operator"]))

    def test_tailscale_installs_crds_without_enabling_api_access_or_proxies(self):
        documents = self.bundles["kubernetes/platform/tailscale"]
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        environment = {entry["name"]: entry.get("value") for entry in container["env"]}
        self.assertEqual(environment["OPERATOR_HOSTNAME"], "tailscale-operator")
        self.assertEqual(environment["OPERATOR_INITIAL_TAGS"], "tag:k8s-operator")
        self.assertEqual(environment["APISERVER_PROXY"], "false")
        self.assertFalse(any(
            "impersonate" in rule["verbs"]
            for doc in documents if doc["kind"] in {"Role", "ClusterRole"}
            for rule in doc.get("rules", [])
        ))
        crds = {doc["metadata"]["name"] for doc in documents if doc["kind"] == "CustomResourceDefinition"}
        self.assertTrue({
            "proxyclasses.tailscale.com", "connectors.tailscale.com",
            "proxygroups.tailscale.com", "dnsconfigs.tailscale.com",
            "recorders.tailscale.com", "tailnets.tailscale.com",
        } <= crds)
        ingress_class = next(doc for doc in documents if doc["kind"] == "IngressClass")
        self.assertEqual(ingress_class["metadata"]["name"], "tailscale")
        self.assertNotEqual(ingress_class["metadata"].get("annotations", {}).get(
            "ingressclass.kubernetes.io/is-default-class"
        ), "true")
        self.assertFalse(any(doc["apiVersion"].startswith("tailscale.com/") for doc in documents))
        self.assertFalse(any(doc["kind"] in {"Ingress", "StatefulSet"} for doc in documents))

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
        ingress_hosts = {
            rule["host"]
            for route in routes if route["kind"] == "Ingress"
            for rule in route["spec"]["rules"]
        }
        self.assertEqual(ingress_hosts, {
            "auth.nullmonkeys.com", "grafana.nullmonkeys.com",
            "longhorn.nullmonkeys.com", "pelican.nullmonkeys.com",
        })
        ingress_routes = [route for route in routes if route["kind"] == "IngressRoute"]
        self.assertTrue(ingress_routes)
        for route in ingress_routes:
            for rule in route["spec"]["routes"]:
                self.assertIn("Host(`argocd.nullmonkeys.com`)", rule["match"])
        for route in routes:
            self.assertEqual(
                route["metadata"]["annotations"]["external-dns.kubernetes.io/target"],
                hostname,
            )

    def test_pelican_syncs_secrets_then_database_and_redis_before_the_panel(self):
        documents = self.bundles["kubernetes/workloads/pelican-panel"]
        sync = next(doc for doc in documents if doc["kind"] == "InfisicalStaticSecret")
        cluster = next(doc for doc in documents if doc["kind"] == "Cluster")
        panel = next(doc for doc in documents if doc["kind"] == "Deployment")
        redis_resources = [
            doc for doc in documents
            if doc["metadata"].get("labels", {}).get("app.kubernetes.io/instance") == "redis"
        ]

        def wave(document):
            return int(document["metadata"].get("annotations", {}).get(
                "argocd.argoproj.io/sync-wave", "0"
            ))

        self.assertLess(wave(sync), wave(cluster))
        self.assertTrue(redis_resources)
        self.assertIn("StatefulSet", {doc["kind"] for doc in redis_resources})
        for document in redis_resources:
            with self.subTest(kind=document["kind"], name=document["metadata"]["name"]):
                self.assertEqual(wave(document), wave(cluster))
                self.assertLess(wave(document), wave(panel))
        for document in documents:
            if document["kind"] == "PersistentVolumeClaim":
                self.assertLess(wave(document), wave(panel))

    def test_pelican_game_database_uses_private_tailscale_exposure_and_managed_credentials(self):
        documents = self.bundles["kubernetes/workloads/pelican-panel"]
        database = next(doc for doc in documents if doc["kind"] == "MariaDB")
        user = next(doc for doc in documents if doc["kind"] == "User")
        grant = next(doc for doc in documents if doc["kind"] == "Grant")
        sync = next(doc for doc in documents if doc["kind"] == "InfisicalStaticSecret")
        service = database["spec"]["service"]
        self.assertEqual(service["type"], "LoadBalancer")
        self.assertEqual(service["loadBalancerClass"], "tailscale")
        self.assertFalse(service["allocateLoadBalancerNodePorts"])
        self.assertEqual(
            service["metadata"]["annotations"]["tailscale.com/hostname"],
            database["metadata"]["name"],
        )
        targets = {target["name"] for target in sync["spec"]["targets"]}
        for document, field, key in (
            (database, "rootPasswordSecretKeyRef", "mariadb-root-password"),
            (user, "passwordSecretKeyRef", "mariadb-pelican-password"),
        ):
            reference = document["spec"][field]
            self.assertIn(reference["name"], targets)
            self.assertEqual(reference["key"], key)
            self.assertFalse(reference.get("generate", False))
        for document in (user, grant):
            self.assertEqual(document["metadata"]["namespace"], database["metadata"]["namespace"])
            self.assertEqual(document["spec"]["mariaDbRef"]["name"], database["metadata"]["name"])
        self.assertEqual(grant["spec"]["username"], user["spec"].get("name", user["metadata"]["name"]))
        self.assertTrue(database["spec"]["metrics"]["enabled"])
        self.assertLess(
            int(sync["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
            int(database["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]),
        )

    def test_pelican_rollouts_keep_a_single_writer_for_rwo_storage(self):
        documents = self.bundles["kubernetes/workloads/pelican-panel"]
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["securityContext"]["fsGroup"], 82)
        claims = {
            doc["metadata"]["name"]: doc["spec"] for doc in documents
            if doc["kind"] == "PersistentVolumeClaim"
        }
        self.assertEqual(set(claims), {
            volume["persistentVolumeClaim"]["claimName"]
            for volume in pod["volumes"]
        })
        for claim in claims.values():
            self.assertEqual(claim["accessModes"], ["ReadWriteOnce"])
            self.assertEqual(claim["storageClassName"], "longhorn")

    def test_pelican_health_checks_wait_for_startup_before_serving_traffic(self):
        documents = self.bundles["kubernetes/workloads/pelican-panel"]
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], "ghcr.io/pelican/panel:v1.0.0-rc.2")
        environment = {entry["name"]: entry.get("value") for entry in container["env"]}
        self.assertEqual(environment["APP_INSTALLED"], "true")
        self.assertEqual(environment["BEHIND_PROXY"], "true")
        ports = {port["name"]: port["containerPort"] for port in container["ports"]}
        for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
            probe = container[probe_name]
            self.assertEqual(probe["httpGet"]["path"], "/up")
            self.assertEqual(ports[probe["httpGet"]["port"]], 80)
        startup = container["startupProbe"]
        self.assertGreaterEqual(startup["periodSeconds"] * startup["failureThreshold"], 600)

    def test_pelican_redis_queue_has_a_small_persistent_data_volume(self):
        documents = self.bundles["kubernetes/workloads/pelican-panel"]
        redis = next(doc for doc in documents if doc["kind"] == "StatefulSet")
        claims = redis["spec"]["volumeClaimTemplates"]
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(claim["spec"]["storageClassName"], "longhorn")
        self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "512Mi")
        container = redis["spec"]["template"]["spec"]["containers"][0]
        self.assertIn(
            {"name": claim["metadata"]["name"], "mountPath": "/data"},
            container["volumeMounts"],
        )

    def test_longhorn_auth_covers_ui_and_api_without_intercepting_login_callbacks(self):
        longhorn = self.bundles["kubernetes/platform/longhorn"]
        authentik = self.bundles["kubernetes/workloads/authentik"]
        ingress = next(doc for doc in longhorn if doc["kind"] == "Ingress")
        middleware = next(doc for doc in longhorn if doc["kind"] == "Middleware")
        namespace = ingress["metadata"]["namespace"]
        self.assertEqual(middleware["metadata"]["namespace"], namespace)
        annotations = ingress["metadata"]["annotations"]
        self.assertEqual(
            annotations["traefik.ingress.kubernetes.io/router.middlewares"],
            f"{namespace}-{middleware['metadata']['name']}@kubernetescrd",
        )
        self.assertEqual(
            annotations["traefik.ingress.kubernetes.io/router.entrypoints"], "websecure"
        )
        rule = ingress["spec"]["rules"][0]
        self.assertEqual(
            [(path["path"], path["pathType"]) for path in rule["http"]["paths"]],
            [("/", "Prefix")],
        )
        callback = next(
            doc for doc in authentik if doc["kind"] == "Ingress"
            and doc["metadata"]["name"] == "longhorn-outpost"
        )
        callback_annotations = callback["metadata"]["annotations"]
        self.assertNotIn(
            "traefik.ingress.kubernetes.io/router.middlewares", callback_annotations
        )
        self.assertGreater(
            int(callback_annotations["traefik.ingress.kubernetes.io/router.priority"]),
            int(annotations["traefik.ingress.kubernetes.io/router.priority"]),
        )
        self.assertEqual(callback["spec"]["ingressClassName"], "traefik")
        self.assertEqual(
            callback_annotations["traefik.ingress.kubernetes.io/router.entrypoints"],
            "websecure",
        )
        self.assertEqual(len(callback["spec"]["rules"]), 1)
        callback_rule = callback["spec"]["rules"][0]
        self.assertEqual(callback_rule["host"], rule["host"])
        self.assertEqual(callback["spec"]["tls"][0]["hosts"], [rule["host"]])
        self.assertEqual(len(callback_rule["http"]["paths"]), 1)
        path = callback_rule["http"]["paths"][0]
        self.assertEqual(path["path"], "/outpost.goauthentik.io/")
        self.assertEqual(path["pathType"], "Prefix")
        backend = path["backend"]["service"]
        service = next(
            doc for doc in authentik if doc["kind"] == "Service"
            and doc["metadata"]["name"] == backend["name"]
        )
        self.assertEqual(service["metadata"]["namespace"], callback["metadata"]["namespace"])
        port = backend["port"]["number"]
        self.assertIn(port, [item["port"] for item in service["spec"]["ports"]])
        self.assertEqual(
            middleware["spec"]["forwardAuth"]["address"],
            f"http://{backend['name']}.{service['metadata']['namespace']}"
            f".svc.cluster.local:{port}/outpost.goauthentik.io/auth/traefik",
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

    def test_authentik_credentials_sync_before_database_recovery_and_deployments(self):
        documents = self.bundles["kubernetes/workloads/authentik"]
        sync = next(doc for doc in documents if doc["kind"] == "InfisicalStaticSecret")
        cluster = next(doc for doc in documents if doc["kind"] == "Cluster")

        def wave(document):
            return int(document["metadata"].get("annotations", {}).get(
                "argocd.argoproj.io/sync-wave", "0"
            ))

        targets = {target["name"] for target in sync["spec"]["targets"]}
        spec = cluster["spec"]
        recovery_source = next(
            source for source in spec["externalClusters"]
            if source["name"] == spec["bootstrap"]["recovery"]["source"]
        )
        for archive in (spec["backup"], recovery_source):
            for reference in archive["barmanObjectStore"]["s3Credentials"].values():
                self.assertIn(reference["name"], targets)
        self.assertLess(wave(sync), wave(cluster))
        deployments = [doc for doc in documents if doc["kind"] == "Deployment"]
        self.assertTrue(deployments)
        for deployment in deployments:
            self.assertLess(wave(cluster), wave(deployment))

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
        self.assertEqual({service["metadata"]["name"] for service in services}, {"traefik-dns", "traefik-metrics"})
        dns = next(service for service in services if service["metadata"]["name"] == "traefik-dns")
        self.assertEqual(dns["spec"]["clusterIP"], "None")
        metrics = next(service for service in services if service["metadata"]["name"] == "traefik-metrics")
        self.assertEqual(metrics["spec"].get("type", "ClusterIP"), "ClusterIP")

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

    def test_arc_runner_namespace_and_dashboard_discovery_labels(self):
        application = next(
            doc for doc in self.bundles["kubernetes/bootstrap/applications"]
            if doc["metadata"]["name"] == "arc-runner-set"
        )
        runners = self.bundles[application["spec"]["source"]["path"]]
        runner_set = next(doc for doc in runners if doc["kind"] == "AutoscalingRunnerSet")
        secret = next(doc for doc in runners if doc["kind"] == "InfisicalStaticSecret")
        namespace = runner_set["metadata"]["namespace"]
        self.assertEqual(application["spec"]["destination"]["namespace"], namespace)
        self.assertEqual(secret["spec"]["targets"][0]["namespace"], namespace)
        self.assertEqual(
            secret["spec"]["targets"][0]["name"], runner_set["spec"]["githubConfigSecret"]
        )

        listener = next(
            doc for doc in self.bundles["kubernetes/platform/arc-systems"]
            if doc["kind"] == "PodMonitor"
            and doc["spec"]["selector"]["matchLabels"].get("app.kubernetes.io/component")
            == "runner-scale-set-listener"
        )
        endpoint = listener["spec"]["podMetricsEndpoints"][0]
        relabel = next(
            rule for rule in endpoint["relabelings"]
            if rule["targetLabel"] == "runner_scale_set_name"
        )
        # ARC propagates the scale-set label to its dynamically created listener pod.
        discovery_labels = {
            "__meta_kubernetes_pod_label_" + re.sub(r"[^a-zA-Z0-9_]", "_", key): value
            for key, value in runner_set["metadata"]["labels"].items()
        }
        source = ";".join(discovery_labels.get(key, "") for key in relabel["sourceLabels"])
        self.assertEqual(source, runner_set["metadata"]["name"])

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
        self.assertLess(waves["cert-manager"], waves["mariadb-operator"])
        self.assertLess(waves["kube-prometheus-stack"], waves["mariadb-operator"])
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
        self.assertEqual(job["scrape_interval"], "30s")
        self.assertEqual(job["kubernetes_sd_configs"], [{"role": "node"}])
        kubelet = next(
            doc for doc in documents
            if doc["kind"] == "ServiceMonitor"
            and doc["metadata"]["name"].endswith("-kubelet")
        )
        cadvisor = next(
            endpoint for endpoint in kubelet["spec"]["endpoints"]
            if endpoint.get("path") == "/metrics/cadvisor"
        )
        self.assertEqual(cadvisor["interval"], "30s")
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

    def test_service_charts_expose_monitors_for_real_metrics_endpoints(self):
        namespaces = {
            app["spec"]["source"]["path"]: app["spec"]["destination"]["namespace"]
            for app in self.bundles["kubernetes/bootstrap/applications"]
        }
        # Argo assigns the Application destination to namespace-less resources.
        targets = [
            dict(doc, metadata=dict(
                doc["metadata"],
                namespace=doc["metadata"].get("namespace", namespaces.get(path, "default")),
            ))
            for path, bundle in self.bundles.items() for doc in bundle
            if doc["kind"] in {"Service", "Deployment", "StatefulSet", "DaemonSet"}
        ]
        for service in (
            "longhorn", "traefik", "cert-manager", "external-dns", "argocd",
            "mariadb-operator",
        ):
            monitors = [
                doc for doc in self.bundles[f"kubernetes/platform/{service}"]
                if doc["kind"] in {"ServiceMonitor", "PodMonitor"}
            ]
            self.assertTrue(monitors, f"{service} must expose a monitor")
            for monitor in monitors:
                with self.subTest(service=service, monitor=monitor["metadata"]["name"]):
                    spec = monitor["spec"]
                    selected_namespaces = spec.get("namespaceSelector", {}).get(
                        "matchNames", [monitor["metadata"].get(
                            "namespace", namespaces[f"kubernetes/platform/{service}"]
                        )]
                    )
                    pod_monitor = monitor["kind"] == "PodMonitor"
                    selected = []
                    for target in targets:
                        if (target["kind"] == "Service") == pod_monitor:
                            continue
                        if target["metadata"].get("namespace", "default") not in selected_namespaces:
                            continue
                        labels = (
                            target["spec"]["template"]["metadata"]["labels"]
                            if pod_monitor else target["metadata"].get("labels", {})
                        )
                        selector = spec["selector"]
                        if any(labels.get(k) != v for k, v in selector.get("matchLabels", {}).items()):
                            continue
                        if any(
                            expression["operator"] != "In"
                            or labels.get(expression["key"]) not in expression["values"]
                            for expression in selector.get("matchExpressions", [])
                        ):
                            continue
                        selected.append(target)
                    if not pod_monitor:
                        # Prometheus ignores Services without the endpoint's named port.
                        selected = [target for target in selected if any(
                            "targetPort" in endpoint
                            or endpoint["port"] in {
                                port.get("name") for port in target["spec"]["ports"]
                            }
                            for endpoint in spec["endpoints"]
                        )]
                    self.assertTrue(selected, "Monitor must select a rendered metrics target")
                    for target in selected:
                        ports = (
                            [port for container in target["spec"]["template"]["spec"]["containers"]
                             for port in container.get("ports", [])]
                            if pod_monitor else target["spec"]["ports"]
                        )
                        for endpoint in spec["podMetricsEndpoints" if pod_monitor else "endpoints"]:
                            if "port" in endpoint:
                                self.assertIn(endpoint["port"], {port.get("name") for port in ports})
                            else:
                                selected_workloads = [
                                    workload for workload in targets
                                    if workload["kind"] != "Service"
                                    and workload["metadata"].get("namespace", "default")
                                    == target["metadata"].get("namespace", "default")
                                    and all(
                                        workload["spec"]["template"]["metadata"]["labels"].get(key) == value
                                        for key, value in target["spec"]["selector"].items()
                                    )
                                ]
                                self.assertTrue(selected_workloads)
                                for workload in selected_workloads:
                                    container_ports = [
                                        port for container in workload["spec"]["template"]["spec"]["containers"]
                                        for port in container.get("ports", [])
                                    ]
                                    field = "name" if isinstance(endpoint["targetPort"], str) else "containerPort"
                                    self.assertIn(endpoint["targetPort"], {port[field] for port in container_ports})
                    if service == "argocd":
                        self.assertEqual(len(selected), 6)
                    if service == "cert-manager":
                        self.assertTrue(all(endpoint["honorLabels"] for endpoint in spec["endpoints"]))

    def test_monitoring_bootstrap_seeds_the_same_api_schemas_before_applications(self):
        import yaml

        prerequisites = self.bundles["kubernetes/bootstrap/monitoring-crds"]
        expected = {"servicemonitors.monitoring.coreos.com", "podmonitors.monitoring.coreos.com"}
        self.assertEqual({doc["metadata"]["name"] for doc in prerequisites}, expected)
        stack_crds = {
            doc["metadata"]["name"]: doc
            for doc in self.bundles["kubernetes/workloads/kube-prometheus-stack"]
            if doc["kind"] == "CustomResourceDefinition"
        }
        for crd in prerequisites:
            self.assertEqual(crd["spec"], stack_crds[crd["metadata"]["name"]]["spec"])
        tasks = yaml.safe_load((ROOT / "ansible/roles/argocd/tasks/main.yml").read_text())
        names = [task["name"] for task in tasks]
        seed = "Seed missing monitoring API prerequisites before child Applications"
        wait = "Wait for monitoring API prerequisites"
        apply = "Apply bootstrap rendering support, projects, and the root Application"
        self.assertLess(names.index(seed), names.index(wait))
        self.assertLess(names.index(wait), names.index(apply))
        seed_task = tasks[names.index(seed)]
        self.assertEqual(seed_task["when"], "argocd_monitoring_crds.stdout_lines | length < 2")
        self.assertNotIn("--force-conflicts", seed_task["ansible.builtin.command"]["argv"])

    def test_chart_downloads_pinned_dashboard_ids_with_separate_providers(self):
        import yaml

        documents = self.bundles["kubernetes/workloads/kube-prometheus-stack"]
        config = next(
            doc for doc in documents if doc["kind"] == "ConfigMap"
            and "download_dashboards.sh" in doc.get("data", {})
        )
        script = config["data"]["download_dashboards.sh"]
        self.assertIn("https://grafana.com/api/dashboards/17346/revisions/9/download", script)
        self.assertIn("https://grafana.com/api/dashboards/14584/revisions/1/download", script)
        self.assertIn("https://grafana.com/api/dashboards/25015/revisions/4/download", script)
        self.assertIn("https://grafana.com/api/dashboards/14057/revisions/1/download", script)
        self.assertIn("curl -fLsS", script)
        provider = yaml.safe_load(config["data"]["dashboardproviders.yaml"])["providers"][0]
        self.assertEqual(provider["options"]["path"], "/var/lib/grafana/dashboards/community")
        self.assertEqual(provider["folder"], "Platform")
        sidecar_provider = next(
            yaml.safe_load(doc["data"]["provider.yaml"])["providers"][0]
            for doc in documents if doc["kind"] == "ConfigMap"
            and "provider.yaml" in doc.get("data", {})
        )
        self.assertNotEqual(provider["options"]["path"], sidecar_provider["options"]["path"])
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment"
                          and doc["metadata"]["name"].endswith("-grafana"))
        pod = deployment["spec"]["template"]["spec"]
        self.assertIn("download-dashboards", {container["name"] for container in pod["initContainers"]})
        grafana = next(container for container in pod["containers"] if container["name"] == "grafana")
        paths = [mount["mountPath"] for mount in grafana["volumeMounts"]]
        self.assertEqual(len(paths), len(set(paths)), "Dashboard providers must not collide")

    def test_discord_credentials_are_synced_and_mounted_without_exposing_the_url(self):
        import yaml

        documents = self.bundles["kubernetes/workloads/kube-prometheus-stack"]
        alertmanager = next(doc for doc in documents if doc["kind"] == "Alertmanager")
        sync = next(doc for doc in documents if doc["kind"] == "InfisicalStaticSecret"
                    and doc["metadata"]["name"] == "alertmanager-notifications-sync")
        target = sync["spec"]["targets"][0]
        self.assertEqual(target["name"], alertmanager["spec"]["secrets"][0])
        self.assertEqual(target["namespace"], alertmanager["metadata"]["namespace"])
        self.assertEqual(sync["spec"]["sources"][0]["secretPath"], "/k3s/alertmanager")
        self.assertLess(int(sync["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]), 0)
        secret = next(doc for doc in documents if doc["kind"] == "Secret"
                      and "alertmanager.yaml" in doc.get("data", {}))
        config = yaml.safe_load(base64.b64decode(secret["data"]["alertmanager.yaml"]))
        receiver = next(receiver for receiver in config["receivers"] if receiver["name"] == "discord")
        discord = receiver["discord_configs"][0]
        self.assertNotIn("webhook_url", discord)
        self.assertEqual(discord["webhook_url_file"], f"/etc/alertmanager/secrets/{target['name']}/discord-webhook-url")
        self.assertTrue(discord["send_resolved"])
        storage = alertmanager["spec"]["storage"]["volumeClaimTemplate"]["spec"]
        self.assertEqual(storage["storageClassName"], "longhorn")
        self.assertEqual(storage["resources"]["requests"]["storage"], "1Gi")
        if os.environ.get("AMTOOL_BIN"):
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "alertmanager.yaml"
                path.write_text(yaml.safe_dump(config))
                subprocess.run([os.environ["AMTOOL_BIN"], "check-config", str(path)], check=True, capture_output=True, text=True)
                for labels, receiver in (
                    (["alertname=Watchdog", "severity=none"], "null"),
                    (["alertname=InfoInhibitor", "severity=info"], "null"),
                    (["alertname=SyntheticInfo", "severity=info"], "null"),
                    (["alertname=LonghornVolumeDegraded", "severity=warning"], "discord"),
                    (["alertname=LonghornVolumeFaulted", "severity=critical"], "discord"),
                ):
                    subprocess.run([
                        os.environ["AMTOOL_BIN"], "config", "routes", "test",
                        f"--config.file={path}", f"--verify.receivers={receiver}", *labels,
                    ], check=True, capture_output=True, text=True)

    @unittest.skipUnless(os.environ.get("PROMTOOL_BIN"), "Set PROMTOOL_BIN for custom alert rule evaluation.")
    def test_platform_alerts_with_prometheus(self):
        import yaml

        documents = self.bundles["kubernetes/workloads/kube-prometheus-stack"]
        rule = next(doc for doc in documents if doc["kind"] == "PrometheusRule" and doc["metadata"]["name"] == "kube-prometheus-stack-platform")
        prometheus = next(doc for doc in documents if doc["kind"] == "Prometheus")
        self.assertEqual(rule["metadata"]["labels"]["release"], prometheus["spec"]["ruleSelector"]["matchLabels"]["release"])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rules_path = directory / "platform.yaml"
            rules_path.write_text(yaml.safe_dump(rule["spec"]))
            subprocess.run([os.environ["PROMTOOL_BIN"], "check", "rules", str(rules_path)], check=True, capture_output=True, text=True)
            fixtures = yaml.safe_load((ROOT / "scripts/tests/fixtures/monitoring-alerts.yaml").read_text())
            fixtures["rule_files"] = [str(rules_path)]
            fixture_path = directory / "tests.yaml"
            fixture_path.write_text(yaml.safe_dump(fixtures))
            subprocess.run([os.environ["PROMTOOL_BIN"], "test", "rules", str(fixture_path)], check=True, capture_output=True, text=True)

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
