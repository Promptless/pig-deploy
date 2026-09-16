"""Verify shared analyzer and maintenance scheduling at the Kubernetes boundary."""

from pathlib import Path

import pytest
import yaml
from pig_supervisor.models import DeploymentSpec, Release
from pig_supervisor.workloads import analyzer_resources, job_resource

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
            "analyzerImage": "ghcr.io/promptless/instruction-hub-worker@sha256:" + "a" * 64,
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
