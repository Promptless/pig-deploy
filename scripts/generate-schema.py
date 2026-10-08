"""Generate the Kubernetes structural schema from the reconciler's public input model."""

import json
from pathlib import Path

import yaml
from pig_supervisor.models import DeploymentSpec, Release
from pig_supervisor.publication import AcceptanceEvidence


def structural(schema, definitions):
    if isinstance(schema, list):
        return [structural(item, definitions) for item in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        return structural(definitions[schema["$ref"].split("/")[-1]], definitions)
    value = {
        key: structural(item, definitions) for key, item in schema.items() if key not in ("$defs", "title", "default")
    }
    if value.get("additionalProperties") is False:
        value.pop("additionalProperties")  # Kubernetes prunes unknown fields; false is not structural.
    if "const" in value:
        value["enum"] = [value.pop("const")]
    if "anyOf" in value:
        variants = [variant for variant in value["anyOf"] if variant.get("type") != "null"]
        if len(variants) == 1:
            value.pop("anyOf")
            value.update(variants[0], nullable=True)
    if "exclusiveMinimum" in value:
        value["minimum"] = value["exclusiveMinimum"]
        value["exclusiveMinimum"] = True
    return value


root = Path(__file__).resolve().parents[1]
source = DeploymentSpec.model_json_schema(by_alias=True)
spec = structural(source, source["$defs"])
spec["properties"]["storage"]["x-kubernetes-validations"] = [
    {
        "rule": "(has(self.s3) ? 1 : 0) + (has(self.azureBlob) ? 1 : 0) + (has(self.gcs) ? 1 : 0) == 1",
        "message": "exactly one native object backend is required",
    }
]
schema = {
    "type": "object",
    "properties": {
        "apiVersion": {"type": "string"},
        "kind": {"type": "string"},
        "metadata": {"type": "object"},
        "spec": spec,
        "status": {"type": "object", "x-kubernetes-preserve-unknown-fields": True},
    },
    "required": ["spec"],
}
crd = {
    "apiVersion": "apiextensions.k8s.io/v1",
    "kind": "CustomResourceDefinition",
    "metadata": {"name": "pigdeployments.governance.promptless.ai"},
    "spec": {
        "group": "governance.promptless.ai",
        "scope": "Namespaced",
        "names": {
            "plural": "pigdeployments",
            "singular": "pigdeployment",
            "kind": "PIGDeployment",
            "shortNames": ["pig"],
        },
        "versions": [
            {
                "name": "v1alpha1",
                "served": True,
                "storage": True,
                "schema": {"openAPIV3Schema": schema},
                "subresources": {"status": {}},
                "additionalPrinterColumns": [
                    {"name": "Version", "type": "string", "jsonPath": ".status.currentVersion"},
                    {"name": "Phase", "type": "string", "jsonPath": ".status.phase"},
                ],
            }
        ],
    },
}
(root / "charts/pig-supervisor/crds/pigdeployments.yaml").write_text(yaml.safe_dump(crd, sort_keys=False))
(root / "schemas/pigdeployment-spec.json").write_text(json.dumps(source, indent=2) + "\n")
(root / "schemas/release.json").write_text(json.dumps(Release.model_json_schema(by_alias=True), indent=2) + "\n")
(root / "schemas/acceptance.json").write_text(
    json.dumps(AcceptanceEvidence.model_json_schema(by_alias=True), indent=2) + "\n"
)
