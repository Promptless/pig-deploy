"""Render actual charts and verify identities, TLS mounts, traffic and RBAC boundaries."""

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64


def render(chart, values, tmp_path):
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values))
    subprocess.run(
        ["helm", "lint", str(ROOT / "charts" / chart), "-f", str(path)], check=True, capture_output=True, text=True
    )
    output = subprocess.run(
        [
            "helm",
            "template",
            "acme",
            str(ROOT / "charts" / chart),
            "--namespace",
            "pig-system",
            "--include-crds",
            "-f",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return list(yaml.safe_load_all(output.stdout))


@pytest.mark.parametrize(
    "backend,storage,expected",
    [
        ("postgres_s3", {"traceObjectS3Bucket": "acme-traces"}, "INSTRUCTION_HUB_TRACE_OBJECT_S3_BUCKET"),
        (
            "postgres_azure_blob",
            {
                "traceObjectAzureAccountUrl": "https://acmetrace.blob.core.windows.net",
                "traceObjectAzureContainer": "traces",
            },
            "INSTRUCTION_HUB_TRACE_OBJECT_AZURE_CONTAINER",
        ),
        ("postgres_gcs", {"traceObjectGcsBucket": "acme-traces"}, "INSTRUCTION_HUB_TRACE_OBJECT_GCS_BUCKET"),
    ],
)
def test_manual_native_identity_ca_and_traffic(backend, storage, expected, tmp_path):
    docs = render(
        "instruction-hub-worker",
        {
            "image": {"digest": DIGEST},
            "serviceAccount": {"create": False, "name": "pig-analyzer"},
            "migrationJob": {"serviceAccountName": "pig-analyzer"},
            "podLabels": {"azure.workload.identity/use": "true"},
            "instructionHub": {
                "runtimeBaseUrl": "https://runtime.example.com",
                "deploymentInstanceId": "test",
                "configHash": "test",
                "storageBackend": backend,
                "postgresCaConfigMapName": "postgres-ca",
                **storage,
            },
            "secrets": {"existingSecretName": "pig-credentials"},
        },
        tmp_path,
    )
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    job = next(d for d in docs if d["kind"] == "Job")
    service = next(d for d in docs if d["kind"] == "Service")
    for doc in (deployment, job):
        template = doc["spec"]["template"]
        assert template["spec"]["serviceAccountName"] == "pig-analyzer"
        assert template["metadata"]["labels"]["azure.workload.identity/use"] == "true"
        container = template["spec"]["containers"][0]
        env = {e["name"]: e for e in container["env"]}
        assert expected in env
        assert env["PGSSLROOTCERT"]["value"] == "/etc/pig/postgres-ca/ca.pem"
        assert container["image"].endswith("@" + DIGEST)
        assert any(v["name"] == "postgres-ca" for v in template["spec"]["volumes"])
    assert service["spec"]["selector"]["app.kubernetes.io/component"] == "analyzer"
    assert job["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] == "maintenance"
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert job["spec"]["backoffLimit"] == 0


def test_supervisor_grants_no_identity_secret_or_rbac_writes(tmp_path):
    docs = render("pig-supervisor", {"image": {"digest": DIGEST}}, tmp_path)
    for doc in docs:
        if doc["kind"] not in ("Role", "ClusterRole"):
            continue
        for rule in doc["rules"]:
            assert "*" not in rule.get("resources", []) + rule["verbs"]
            if set(rule["verbs"]) - {"get", "list", "watch"}:
                assert not set(rule.get("resources", [])) & {
                    "secrets",
                    "serviceaccounts",
                    "roles",
                    "rolebindings",
                    "clusterroles",
                    "clusterrolebindings",
                }
            if "customresourcedefinitions" in rule.get("resources", []):
                assert rule["resourceNames"] == ["pigdeployments.governance.promptless.ai"]
    controller = next(d for d in docs if d["kind"] == "Deployment")
    assert controller["spec"]["strategy"]["type"] == "Recreate"
