"""Exercise the shipped chart and REST client against Kubernetes, without cloud or analyzer credentials."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from pig_supervisor.models import DeploymentSpec

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = "kind-pig-ci"
NAMESPACE = "pig-ci"
SYSTEM = "pig-ci-system"
NAME = "pig-supervisor"


def command(*args: str, document: dict | None = None) -> str:
    result = subprocess.run(
        args,
        input=json.dumps(document) if document is not None else None,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, f"{args[0]} failed: {result.stderr}\n{result.stdout}"
    return result.stdout


def kubectl(*args: str, document: dict | None = None) -> str:
    return command("kubectl", "--context", CONTEXT, *args, document=document)


def apply(document: dict) -> dict:
    return json.loads(kubectl("apply", "-f", "-", "-o", "json", document=document))


def read(kind: str, name: str, namespace: str = NAMESPACE) -> dict:
    return json.loads(kubectl("get", kind, name, "-n", namespace, "-o", "json"))


def wait_for(description, predicate, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(1)
    pytest.fail(f"Timed out waiting for {description}")


def helm(*args: str) -> str:
    repository, digest = os.environ["PIG_TEST_IMAGE"].split("@")
    return command(
        "helm",
        "upgrade",
        "--install",
        NAME,
        str(ROOT / "charts/pig-supervisor"),
        "--kube-context",
        CONTEXT,
        "--namespace",
        SYSTEM,
        "--create-namespace",
        "--set-string",
        f"watchNamespace={NAMESPACE}",
        "--set-string",
        f"image.repository={repository}",
        "--set-string",
        f"image.digest={digest}",
        # A paused deployment must not need a reachable catalog.
        "--set-string",
        "releaseCatalogURL=http://127.0.0.1:9/catalog.json",
        "--wait",
        "--timeout",
        "120s",
        *args,
    )


@pytest.fixture(scope="module", autouse=True)
def cluster():
    assert "pig-ci" in command("kind", "get", "clusters").splitlines()
    assert os.environ.get("PIG_TEST_IMAGE", "").startswith("pig-registry:5001/pig-supervisor@sha256:")
    apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
    helm()
    kubectl("wait", "--for=condition=Established", "crd/pigdeployments.governance.promptless.ai", "--timeout=60s")
    # The workflow deletes the entire kind cluster, including failed-test state.


@pytest.fixture
def deployment():
    document = yaml.safe_load((ROOT / "examples/pig-deployment.yaml").read_text())
    document["metadata"] = {"name": "integration", "namespace": NAMESPACE}
    document["spec"]["release"] = {"paused": True}
    spec = DeploymentSpec.model_validate(document["spec"])
    apply(
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": spec.service_account_name, "namespace": NAMESPACE},
        }
    )
    secrets: dict[str, dict[str, str]] = {}
    refs = [spec.hosted.install_token_secret_ref, spec.storage.postgres.dsn_secret_ref]
    refs += [
        ref for ref in (spec.storage.postgres.migration_dsn_secret_ref, spec.analysis.model.api_key_secret_ref) if ref
    ]
    for ref in refs:
        secrets.setdefault(ref.name, {})[ref.key] = "integration-placeholder"
    for name, data in secrets.items():
        apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": NAMESPACE},
                "stringData": data,
            }
        )
    ca = spec.storage.postgres.ca_config_map_ref
    if ca:
        apply(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": ca.name, "namespace": NAMESPACE},
                "data": {ca.key: "integration-placeholder"},
            }
        )
    result = apply(document)
    yield result
    kubectl("delete", "pigdeployment", "integration", "-n", NAMESPACE, "--wait=true")


def test_chart_install_upgrade_and_process_handoff(deployment):
    helm()

    def paused():
        current = read("pigdeployment", "integration")
        return any(
            c["reason"] == "Paused"
            and c["type"] == "Blocked"
            and c["status"] == "True"
            and c["observedGeneration"] == current["metadata"]["generation"]
            for c in current.get("status", {}).get("conditions", [])
        )

    wait_for("the installed process to reconcile a paused deployment", paused)
    old_holder = read("lease", NAME)["spec"]["holderIdentity"]
    old_uid = deployment["metadata"]["uid"]
    helm("--set", "resources.requests.cpu=110m")
    # The previous Pod has terminated. Shorten the observation wait for this test.
    kubectl(
        "patch",
        "lease",
        NAME,
        "-n",
        NAMESPACE,
        "--type=merge",
        "-p",
        json.dumps({"spec": {"leaseDurationSeconds": 1}}),
    )
    wait_for(
        "the replacement process to acquire leadership",
        lambda: read("lease", NAME)["spec"]["holderIdentity"] != old_holder,
    )
    kubectl(
        "patch",
        "pigdeployment",
        "integration",
        "-n",
        NAMESPACE,
        "--type=merge",
        "-p",
        json.dumps({"spec": {"hosted": {"runtimeURL": "https://staging.example.com"}}}),
    )
    wait_for("the upgraded process to observe the new generation", paused)
    assert read("pigdeployment", "integration")["metadata"]["uid"] == old_uid
    assert json.loads(kubectl("get", "jobs", "-n", NAMESPACE, "-o", "json"))["items"] == []


def test_go_client_rbac_admission_apply_and_leadership(deployment, monkeypatch):
    kubectl("scale", "deployment", NAME, "-n", SYSTEM, "--replicas=0")
    wait_for(
        "supervisor Pods to terminate",
        lambda: not json.loads(kubectl("get", "pods", "-n", SYSTEM, "-o", "json"))["items"],
    )
    kubectl("delete", "lease", NAME, "-n", NAMESPACE, "--ignore-not-found")
    monkeypatch.setenv("PIG_KUBERNETES_TEST", "1")
    command("go", "test", "./supervisor/internal/supervisor", "-run", "^TestKubernetes$", "-count=1", "-v")
