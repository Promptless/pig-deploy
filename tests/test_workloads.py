"""Verify shared analyzer and maintenance scheduling at the Kubernetes boundary."""

import json
from pathlib import Path

import pytest
import yaml
from pig_supervisor.models import DeploymentSpec, Release
from pig_supervisor.workloads import analyzer_resources, job_resource, secret_refs

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("node_selector", [{}, {"iam.gke.io/gke-metadata-server-enabled": "true"}])
def test_all_workloads_use_customer_node_selector(node_selector: dict[str, str]) -> None:
    """Cloud identity constraints apply to the analyzer and every maintenance phase."""
    deployment = yaml.safe_load((ROOT / "examples/pig-deployment.yaml").read_text())
    deployment["metadata"]["uid"] = "example-uid"
    deployment["spec"]["nodeSelector"] = node_selector
    spec = DeploymentSpec.model_validate(deployment["spec"])
    release = Release.model_validate(
        {
            "version": "0.3.0",
            "analyzerImage": "ghcr.io/promptless/pig-trace-analyzer@sha256:" + "a" * 64,
            "supervisorImage": "ghcr.io/promptless/pig-supervisor@sha256:" + "b" * 64,
            "requirements": {"storageBackends": ["s3", "azureBlob", "gcs"], "schemaFrom": [0], "schemaTo": 1},
        }
    )

    resources = analyzer_resources(deployment, spec, release, "config-hash")
    resources += [
        job_resource(deployment, spec, release, "a" * 64, "config-hash", phase, 0)
        for phase in ("preflight", "migration", "verify", "acceptance")
    ]

    for resource in resources:
        if resource["kind"] in {"Deployment", "Job"}:
            assert resource["spec"]["template"]["spec"]["nodeSelector"] == node_selector


def test_migration_credentials_stay_out_of_serving_and_verification() -> None:
    """Only preflight and migration Jobs receive the schema owner's credential."""
    deployment = yaml.safe_load((ROOT / "examples/pig-deployment.yaml").read_text())
    deployment["metadata"]["uid"] = "example-uid"
    ref = {"name": "migration-only", "key": "dsn"}
    deployment["spec"]["storage"]["postgres"]["migrationDsnSecretRef"] = ref
    spec = DeploymentSpec.model_validate(deployment["spec"])
    release = Release.model_validate(
        {
            "version": "0.3.0",
            "analyzerImage": "ghcr.io/promptless/pig-trace-analyzer@sha256:" + "a" * 64,
            "supervisorImage": "ghcr.io/promptless/pig-supervisor@sha256:" + "b" * 64,
            "requirements": {
                "storageBackends": ["s3"],
                "schemaFrom": [0, 1, 2, 3],
                "schemaTo": 4,
                "capabilities": ["storage-readiness-v1"],
            },
        }
    )
    resources = analyzer_resources(deployment, spec, release, "config-hash")
    for resource in resources:
        if resource["kind"] == "Deployment":
            container = resource["spec"]["template"]["spec"]["containers"][0]
            assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
            assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
            assert not any(e["name"] == "INSTRUCTION_HUB_MIGRATION_POSTGRES_DSN" for e in container["env"])
    for phase in ("preflight", "migration", "verify", "acceptance"):
        job = job_resource(deployment, spec, release, "a" * 64, "config-hash", phase, 0)
        container = job["spec"]["template"]["spec"]["containers"][0]
        env = {entry["name"]: entry for entry in container["env"]}
        if phase in ("preflight", "migration"):
            assert env["INSTRUCTION_HUB_MIGRATION_POSTGRES_DSN"]["valueFrom"]["secretKeyRef"] == ref
        else:
            assert "INSTRUCTION_HUB_MIGRATION_POSTGRES_DSN" not in env
        assert "startupProbe" not in container


@pytest.mark.parametrize("authentication", ["api_key", "aws_sigv4"])
def test_hosted_repository_selection_needs_no_customer_repository_credentials(authentication: str) -> None:
    deployment = yaml.safe_load((ROOT / "examples/pig-deployment.yaml").read_text())
    deployment["metadata"]["uid"] = "example-uid"
    if authentication == "aws_sigv4":
        deployment["spec"]["analysis"]["model"] = {
            "provider": "aws_bedrock",
            "authentication": authentication,
            "baseURL": "https://bedrock-mantle.us-east-1.api.aws/v1",
            "name": "test-model",
        }
    spec = DeploymentSpec.model_validate(deployment["spec"])
    release = Release.model_validate(
        {
            "version": "0.3.0",
            "analyzerImage": "ghcr.io/promptless/pig-trace-analyzer@sha256:" + "a" * 64,
            "supervisorImage": "ghcr.io/promptless/pig-supervisor@sha256:" + "b" * 64,
            "requirements": json.loads((ROOT / "releases/requirements/0.3.0.json").read_text()),
        }
    )
    expected_secret_keys = {"install-token", "postgres-dsn"}
    if authentication == "api_key":
        expected_secret_keys.add("model-api-key")
    assert {ref.key for ref in secret_refs(spec)} == expected_secret_keys
    workloads = [analyzer_resources(deployment, spec, release, "config-hash")[0]] + [
        job_resource(deployment, spec, release, "a" * 64, "config-hash", phase, 0)
        for phase in ("preflight", "migration", "verify", "acceptance")
    ]
    for resource in workloads:
        env = resource["spec"]["template"]["spec"]["containers"][0]["env"]
        assert not any(entry["name"].startswith("INSTRUCTION_HUB_ANALYSIS_REPOSITORY_") for entry in env)
        assert {
            entry["valueFrom"]["secretKeyRef"]["key"] for entry in env if "valueFrom" in entry
        } == expected_secret_keys
