"""Render actual charts and verify identities, TLS mounts, traffic and RBAC boundaries."""

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def worker_values() -> dict[str, object]:
    """Supply the required manual-chart settings without changing identity defaults."""
    return {
        "image": {"digest": DIGEST},
        "instructionHub": {
            "configHash": "test",
            "traceObjectS3Bucket": "acme-traces",
        },
        "secrets": {"existingSecretName": "pig-credentials"},
    }


def render(chart, values, tmp_path, release="acme"):
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values))
    subprocess.run(
        ["helm", "lint", str(ROOT / "charts" / chart), "-f", str(path)], check=True, capture_output=True, text=True
    )
    output = subprocess.run(
        [
            "helm",
            "template",
            release,
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


def test_manual_chart_isolates_migration_credentials_and_checks_storage_readiness(worker_values, tmp_path):
    """Rendered serving pods use app credentials and readiness distinct from liveness."""
    worker_values["secrets"] = {"existingSecretName": "pig-credentials", "migrationPostgresDsnKey": "migration-dsn"}
    docs = render("pig-trace-analyzer", worker_values, tmp_path)
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    job = next(doc for doc in docs if doc["kind"] == "Job")
    serving = deployment["spec"]["template"]["spec"]["containers"][0]
    migration = job["spec"]["template"]["spec"]["containers"][0]
    serving_env = {entry["name"]: entry for entry in serving["env"]}
    migration_env = {entry["name"]: entry for entry in migration["env"]}
    assert "INSTRUCTION_HUB_MIGRATION_POSTGRES_DSN" not in serving_env
    assert migration_env["INSTRUCTION_HUB_MIGRATION_POSTGRES_DSN"]["valueFrom"]["secretKeyRef"] == {
        "name": "pig-credentials",
        "key": "migration-dsn",
    }
    assert (
        serving_env["INSTRUCTION_HUB_CUSTOMER_POSTGRES_DSN"] == migration_env["INSTRUCTION_HUB_CUSTOMER_POSTGRES_DSN"]
    )
    assert serving["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert serving["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert serving["startupProbe"]["httpGet"]["path"] == "/healthz"


@pytest.mark.parametrize(
    "release,override",
    [("acme", None), ("customer-production-us-east-2-pig", None), ("a" * 53, None), ("acme", "x" * 54 + "-suffix")],
)
def test_migration_job_name_reserves_room_for_suffix(worker_values, tmp_path, release, override):
    if override:
        worker_values["fullnameOverride"] = override
    docs = render("pig-trace-analyzer", worker_values, tmp_path, release)
    job = next(doc for doc in docs if doc["kind"] == "Job")
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    name = job["metadata"]["name"]
    assert name == deployment["metadata"]["name"][:55].rstrip("-") + "-migrate"
    assert len(name) <= 63


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
        "pig-trace-analyzer",
        {
            "image": {"digest": DIGEST},
            "serviceAccount": {"create": False, "name": "pig-analyzer"},
            "podLabels": {"azure.workload.identity/use": "true"},
            "nodeSelector": {"iam.gke.io/gke-metadata-server-enabled": "true"},
            "instructionHub": {
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
        assert template["spec"]["nodeSelector"] == {"iam.gke.io/gke-metadata-server-enabled": "true"}
        assert template["metadata"]["labels"]["azure.workload.identity/use"] == "true"
        container = template["spec"]["containers"][0]
        env = {e["name"]: e for e in container["env"]}
        assert expected in env
        assert env["INSTRUCTION_HUB_RUNTIME_BASE_URL"]["value"] == "https://api.gopromptless.ai"
        assert "INSTRUCTION_HUB_DEPLOYMENT_INSTANCE_ID" not in env
        assert "INSTRUCTION_HUB_DEPLOYMENT_NAME" not in env
        assert env["INSTRUCTION_HUB_INSTALL_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "pig-credentials"
        assert env["PGSSLROOTCERT"]["value"] == "/etc/pig/postgres-ca/ca.pem"
        assert container["image"].endswith("@" + DIGEST)
        assert any(v["name"] == "postgres-ca" for v in template["spec"]["volumes"])
    assert service["spec"]["selector"]["app.kubernetes.io/component"] == "analyzer"
    assert job["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] == "maintenance"
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert job["spec"]["backoffLimit"] == 0


def test_manual_runtime_override_reaches_analyzer_and_migration(worker_values, tmp_path):
    worker_values["instructionHub"]["runtimeBaseUrl"] = "https://staging.example.com"
    docs = render("pig-trace-analyzer", worker_values, tmp_path)
    for resource in docs:
        if resource["kind"] not in {"Deployment", "Job"}:
            continue
        env = {entry["name"]: entry for entry in resource["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["INSTRUCTION_HUB_RUNTIME_BASE_URL"]["value"] == "https://staging.example.com"


@pytest.mark.parametrize("activation_at", ["", "2026-09-21T00:00:00Z"])
@pytest.mark.parametrize("authentication", ["api_key", "aws_sigv4"])
def test_manual_model_access_without_repository_configuration(
    activation_at: str, authentication: str, tmp_path: Path
) -> None:
    """Analysis and catalog-only operation receive model access and writable mirrors."""
    model = {
        "provider": "openai" if authentication == "api_key" else "aws_bedrock",
        "authentication": authentication,
        "baseUrl": "https://api.openai.com/v1"
        if authentication == "api_key"
        else "https://bedrock-mantle.us-east-1.api.aws/v1",
        "model": "test-model",
    }
    docs = render(
        "pig-trace-analyzer",
        {
            "image": {"digest": DIGEST},
            "instructionHub": {
                "configHash": "test",
                "traceObjectS3Bucket": "acme-traces",
                "analysis": {
                    "activationAt": activation_at,
                    "catalogEnabled": not activation_at,
                    "mirrorRoot": "/var/lib/instruction-hub/custom-mirrors",
                    "modelApi": model,
                },
            },
            "secrets": {
                "create": True,
                "installToken": "test-install-token",
                "customerPostgresDsn": "test-postgres-dsn",
                "analysisModelApiKey": "test-model-key" if authentication == "api_key" else "",
            },
        },
        tmp_path,
    )
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["INSTRUCTION_HUB_ANALYSIS_MODEL_NAME"]["value"] == "test-model"
    assert env["INSTRUCTION_HUB_ANALYSIS_MODEL_AUTHENTICATION"]["value"] == authentication
    assert env["INSTRUCTION_HUB_ANALYSIS_MIRROR_ROOT"]["value"] == "/var/lib/instruction-hub/custom-mirrors"
    assert ("INSTRUCTION_HUB_ANALYSIS_ACTIVATION_AT" in env) is bool(activation_at)
    assert not any(name.startswith("INSTRUCTION_HUB_ANALYSIS_REPOSITORY_") for name in env)
    assert {"name": "analysis-mirrors", "mountPath": "/var/lib/instruction-hub"} in container["volumeMounts"]
    assert {"name": "analysis-mirrors", "emptyDir": {}} in pod["volumes"]
    secret = next(doc for doc in docs if doc["kind"] == "Secret")
    expected_keys = {"install-token", "customer-postgres-dsn"}
    if authentication == "api_key":
        expected_keys.add("analysis-model-api-key")
        assert env["INSTRUCTION_HUB_ANALYSIS_MODEL_API_KEY"]["valueFrom"]["secretKeyRef"] == {
            "name": secret["metadata"]["name"],
            "key": "analysis-model-api-key",
        }
    else:
        assert "INSTRUCTION_HUB_ANALYSIS_MODEL_API_KEY" not in env
    assert set(secret["stringData"]) == expected_keys


def test_manual_default_migration_uses_preexisting_shared_account(
    worker_values: dict[str, object], tmp_path: Path
) -> None:
    """Pre-install migration must use the same external identity as the analyzer."""
    docs = render("pig-trace-analyzer", worker_values, tmp_path)

    assert not any(doc["kind"] == "ServiceAccount" for doc in docs)
    workloads = [doc for doc in docs if doc["kind"] in {"Deployment", "Job"}]
    assert len(workloads) == 2
    for workload in workloads:
        assert workload["spec"]["template"]["spec"]["serviceAccountName"] == "pig-analyzer"
        assert "nodeSelector" not in workload["spec"]["template"]["spec"]


def test_manual_rejects_chart_created_identity_before_migration(
    worker_values: dict[str, object], tmp_path: Path
) -> None:
    """Reject a pre-install Job that would wait for an account installed after it."""
    worker_values["serviceAccount"] = {"create": True, "name": "pig-analyzer"}

    with pytest.raises(subprocess.CalledProcessError) as error:
        render("pig-trace-analyzer", worker_values, tmp_path)

    assert "pre-existing shared ServiceAccount" in (error.value.stdout or "") + (error.value.stderr or "")


def test_manual_can_create_identity_with_external_migrations(worker_values: dict[str, object], tmp_path: Path) -> None:
    """The chart may own its identity when migrations are managed separately."""
    worker_values["serviceAccount"] = {"create": True, "name": "custom-analyzer"}
    worker_values["migrationJob"] = {"enabled": False}

    docs = render("pig-trace-analyzer", worker_values, tmp_path)

    assert not any(doc["kind"] == "Job" for doc in docs)
    account = next(doc for doc in docs if doc["kind"] == "ServiceAccount")
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    assert account["metadata"]["name"] == "custom-analyzer"
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == account["metadata"]["name"]


def test_manual_rejects_separate_migration_identity(worker_values: dict[str, object], tmp_path: Path) -> None:
    """A distinct migration ServiceAccount is outside the shared identity contract."""
    worker_values["migrationJob"] = {"serviceAccountName": "different-account"}

    with pytest.raises(subprocess.CalledProcessError):
        render("pig-trace-analyzer", worker_values, tmp_path)


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
