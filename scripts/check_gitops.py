"""Build Kustomize roots and validate manifests without contacting a cluster.

Requires kubectl, Helm, and kubernetes/requirements.txt, plus kubeconform unless using
--prepare-only for a separate container validation step. Schema downloads use
public sources; custom resource schemas come from the rendered CRDs.
"""

import argparse
import copy
import fnmatch
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class KubernetesLoader(yaml.SafeLoader):
    """Keep bare '=' enum values from rendered CRDs as strings."""


KubernetesLoader.add_constructor(
    "tag:yaml.org,2002:value", yaml.SafeLoader.construct_scalar
)


def build_manifests(root, kubectl, helm="helm"):
    """Use the same Helm-enabled Kustomize build as Argo, with fresh caches."""
    bundles = {}
    kubernetes_version = (
        yaml.safe_load((root / "ansible/vars/k3s.yml").read_text())["k3s_version"]
        .removeprefix("v")
        .split("+")[0]
    )
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        copied = directory / "kubernetes"
        shutil.copytree(
            root / "kubernetes", copied, ignore=shutil.ignore_patterns("charts")
        )
        environment = dict(
            os.environ,
            HELM_CONFIG_HOME=str(directory / "helm-config"),
            HELM_CACHE_HOME=str(directory / "helm-cache"),
            HELM_DATA_HOME=str(directory / "helm-data"),
        )
        for path in sorted(copied.rglob("kustomization.yaml")):
            rendered = subprocess.run(
                [
                    kubectl,
                    "kustomize",
                    str(path.parent),
                    "--enable-helm",
                    "--helm-command",
                    helm,
                    "--helm-kube-version",
                    kubernetes_version,
                ],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                env=environment,
            ).stdout
            name = "kubernetes/" + path.parent.relative_to(copied).as_posix()
            bundles[name] = [
                doc
                for doc in yaml.load_all(rendered, Loader=KubernetesLoader)
                if doc is not None
            ]
    if not bundles:
        raise ValueError("No Kubernetes Kustomize roots were found.")
    return bundles


def validate_references(bundles):
    bootstrap = bundles["kubernetes/bootstrap"]
    projects = {
        doc["metadata"]["name"]: doc["spec"]
        for doc in bootstrap
        if doc["kind"] == "AppProject"
    }
    root = next(
        doc
        for doc in bootstrap
        if doc["kind"] == "Application" and doc["metadata"]["name"] == "cluster"
    )
    definitions = root["spec"]["source"]["path"]
    if definitions != "kubernetes/bootstrap/applications":
        raise ValueError(
            "The root Application must reconcile kubernetes/bootstrap/applications."
        )
    if any(doc["kind"] != "Application" for doc in bundles[definitions]):
        raise ValueError(
            "The root Application may only manage child Application definitions."
        )
    applications = [root, *bundles[definitions]]
    for application in applications:
        spec = application["spec"]
        project = projects[spec["project"]]
        destination = spec["destination"]
        if not any(
            destination["server"] == entry["server"]
            and fnmatch.fnmatchcase(destination["namespace"], entry["namespace"])
            for entry in project["destinations"]
        ):
            raise ValueError(
                f"{application['metadata']['name']}: destination is outside its AppProject."
            )
        for source in spec.get("sources", [spec.get("source", {})]):
            if not any(
                fnmatch.fnmatchcase(source["repoURL"], pattern)
                for pattern in project["sourceRepos"]
            ):
                raise ValueError(
                    f"{application['metadata']['name']}: repository is outside its AppProject."
                )
            if (
                source["repoURL"] == root["spec"]["source"]["repoURL"]
                and "path" in source
                and source["path"] not in bundles
            ):
                raise ValueError(
                    f"{application['metadata']['name']}: source path has no validated Kustomize root."
                )


def export_crd_schemas(documents, directory):
    for document in documents:
        if document["kind"] != "CustomResourceDefinition":
            continue
        spec = document["spec"]
        for version in spec["versions"]:
            schema = copy.deepcopy(version["schema"]["openAPIV3Schema"])
            schema["$schema"] = "http://json-schema.org/draft-04/schema#"
            schema["properties"]["apiVersion"] = {
                "enum": [f"{spec['group']}/{version['name']}"]
            }
            schema["properties"]["kind"] = {"enum": [spec["names"]["kind"]]}
            make_schema_strict(schema)
            target = (
                directory
                / spec["group"]
                / f"{spec['names']['kind'].lower()}_{version['name']}.json"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(schema))


def make_schema_strict(schema):
    """Reject unknown CRD fields, preserving explicitly free-form maps."""
    if isinstance(schema, dict):
        if (
            schema.get("type") == "object"
            and "properties" in schema
            and not schema.get("x-kubernetes-preserve-unknown-fields")
        ):
            schema.setdefault("additionalProperties", False)
        for value in schema.values():
            make_schema_strict(value)
    elif isinstance(schema, list):
        for value in schema:
            make_schema_strict(value)


def export_definition_schema(kubernetes_version, directory):
    """The default schema registry omits CRDs; use Kubernetes' own API schema."""
    url = (
        f"https://raw.githubusercontent.com/kubernetes/kubernetes/v{kubernetes_version}/"
        "api/openapi-spec/v3/apis__apiextensions.k8s.io__v1_openapi.json"
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        schemas = json.load(response)["components"]["schemas"]
    schema = {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "$ref": "#/definitions/io.k8s.apiextensions-apiserver.pkg.apis.apiextensions.v1.CustomResourceDefinition",
        "definitions": schemas,
    }
    make_schema_strict(schema)
    # Keep recursive references (JSONSchemaProps) intact rather than inlining them.
    serialized = json.dumps(schema).replace("#/components/schemas/", "#/definitions/")
    target = directory / "apiextensions.k8s.io/customresourcedefinition_v1.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(serialized)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--kubeconform", default="kubeconform")
    parser.add_argument(
        "--prepare-only",
        type=Path,
        metavar="DIRECTORY",
        help="Write manifests, schemas, and Kubernetes version for separate validation.",
    )
    args = parser.parse_args()
    k3s = yaml.safe_load((ROOT / "ansible/vars/k3s.yml").read_text())
    bundles = build_manifests(ROOT, args.kubectl, args.helm)
    validate_references(bundles)
    kubernetes_version = k3s["k3s_version"].removeprefix("v").split("+")[0]
    with tempfile.TemporaryDirectory() as temporary:
        directory = args.prepare_only or Path(temporary)
        directory.mkdir(parents=True, exist_ok=True)
        export_crd_schemas(
            [doc for documents in bundles.values() for doc in documents],
            directory / "schemas",
        )
        export_definition_schema(kubernetes_version, directory / "schemas")
        resources = [
            document for documents in bundles.values() for document in documents
        ]
        if args.prepare_only:
            (directory / "manifests.yaml").write_text(yaml.safe_dump_all(resources))
            (directory / "kubernetes-version").write_text(kubernetes_version + "\n")
            print(
                f"Prepared {len(bundles)} Kustomize roots for kubeconform validation in {directory}."
            )
            return
        subprocess.run(
            [
                args.kubeconform,
                "-strict",
                "-summary",
                "-kubernetes-version",
                kubernetes_version,
                "-schema-location",
                "default",
                "-schema-location",
                str(
                    directory
                    / "schemas"
                    / "{{.Group}}"
                    / "{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
                ),
                "-",
            ],
            input=yaml.safe_dump_all(resources),
            text=True,
            check=True,
        )
    print(
        f"Validated {len(bundles)} Kustomize roots and their Argo CD source/project references."
    )


if __name__ == "__main__":
    main()
