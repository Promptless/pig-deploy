"""Exercise the shipped chart and REST client against Kubernetes, without cloud or analyzer credentials."""

import base64
import json
import os
import ssl
import subprocess
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml
from pig_supervisor.controller import Controller
from pig_supervisor.kube import Kube, KubeError, resource_path
from pig_supervisor.models import DeploymentSpec
from pig_supervisor.workloads import secret_refs

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
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, f"{args[0]} failed: {result.stderr}"
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
    for ref in secret_refs(spec):
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


@pytest.fixture
def api():
    # Stop the process before exercising independent contenders with its real identity.
    kubectl("scale", "deployment", NAME, "-n", SYSTEM, "--replicas=0")
    wait_for(
        "supervisor Pods to terminate",
        lambda: not json.loads(kubectl("get", "pods", "-n", SYSTEM, "-o", "json"))["items"],
    )
    kubectl("delete", "lease", NAME, "-n", NAMESPACE, "--ignore-not-found")
    config = json.loads(kubectl("config", "view", "--minify", "--raw", "--flatten", "-o", "json"))
    connection = config["clusters"][0]["cluster"]
    context = ssl.create_default_context(cadata=base64.b64decode(connection["certificate-authority-data"]).decode())
    token = kubectl("create", "token", NAME, "-n", SYSTEM, "--duration=10m").strip()
    with httpx.Client(
        base_url=connection["server"], verify=context, headers={"Authorization": f"Bearer {token}"}, timeout=15
    ) as client:
        kube = Kube(client)
        assert kube.leadership(NAMESPACE, "test-holder", datetime.now(UTC))
        yield kube
    kubectl("delete", "lease", NAME, "-n", NAMESPACE, "--ignore-not-found")


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
        json.dumps({"spec": {"hosted": {"deploymentID": "after-upgrade"}}}),
    )
    wait_for("the upgraded process to observe the new generation", paused)
    assert read("pigdeployment", "integration")["metadata"]["uid"] == old_uid
    assert json.loads(kubectl("get", "jobs", "-n", NAMESPACE, "-o", "json"))["items"] == []


def test_admission_status_conflict_and_secret_rotation(api, deployment):
    path = resource_path("PIGDeployment", NAMESPACE, "integration")
    invalid = deepcopy(deployment)
    invalid["spec"]["serviceAccountName"] = "INVALID NAME"
    with pytest.raises(AssertionError, match="spec.serviceAccountName: Invalid value"):
        apply(invalid)
    current = api.get("PIGDeployment", NAMESPACE, "integration")
    api.status(current, {"phase": "preflight", "attempt": 1, "retryAt": "2026-01-01T00:00:00Z"})
    with pytest.raises(KubeError) as conflict:
        api.status(current, {"phase": "quiesce"})
    assert conflict.value.status == 409
    current = api.get("PIGDeployment", NAMESPACE, "integration")
    api.status(current, {"phase": "quiesce"})
    persisted = api.request("GET", path)
    assert persisted["status"] == {"phase": "quiesce"}
    assert persisted["spec"] == current["spec"]

    spec = DeploymentSpec.model_validate(deployment["spec"])
    with httpx.Client() as catalog:
        controller = Controller(api, catalog, "http://127.0.0.1:9", NAMESPACE, SYSTEM, NAME)
        before = controller._configuration_hash(spec)
        ref = spec.hosted.install_token_secret_ref
        kubectl(
            "patch",
            "secret",
            ref.name,
            "-n",
            NAMESPACE,
            "--type=merge",
            "-p",
            json.dumps({"stringData": {ref.key: "rotated-placeholder"}}),
        )
        assert controller._configuration_hash(spec) != before


def test_server_side_apply_preserves_field_and_resource_ownership(api, deployment):
    owner = {
        "apiVersion": deployment["apiVersion"],
        "kind": "PIGDeployment",
        "name": "integration",
        "uid": deployment["metadata"]["uid"],
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "apply-test", "namespace": NAMESPACE, "ownerReferences": [owner]},
        "spec": {"selector": {"app": "first"}, "ports": [{"port": 80, "targetPort": 8080}]},
    }
    first = api.apply(service)
    service["spec"]["selector"]["app"] = "second"
    assert api.apply(service)["spec"]["clusterIP"] == first["spec"]["clusterIP"]
    contender = deepcopy(service)
    contender["spec"]["selector"]["app"] = "other-manager"
    with pytest.raises(KubeError) as conflict:
        api.request(
            "PATCH",
            resource_path("Service", NAMESPACE, "apply-test"),
            json=contender,
            params={"fieldManager": "another-manager", "force": "false"},
            headers={"Content-Type": "application/apply-patch+yaml"},
        )
    assert conflict.value.status == 409
    contender["metadata"]["ownerReferences"][0]["uid"] = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(KubeError, match="different owner"):
        api.apply(contender)
    assert api.get("Service", NAMESPACE, "apply-test")["spec"]["selector"]["app"] == "second"


def test_real_rbac_and_lease_contenders(api, monkeypatch):
    elapsed = time.monotonic()
    monkeypatch.setattr("pig_supervisor.kube.monotonic", lambda: elapsed)
    now = datetime.now(UTC)
    contender = Kube(api.client)
    assert not contender.leadership(NAMESPACE, "second-holder", now)
    with pytest.raises(KubeError, match="Lease expired"):
        contender.patch("Service", NAMESPACE, "anything", {"spec": {"selector": {"app": "unauthorized"}}})
    assert not contender.leadership(NAMESPACE, "second-holder", now + timedelta(days=1))
    elapsed += 301
    assert contender.leadership(NAMESPACE, "second-holder", now + timedelta(seconds=301))
    with pytest.raises(KubeError, match="Lease expired"):
        api.patch("Service", NAMESPACE, "anything", {"spec": {"selector": {"app": "expired"}}})
    assert not api.leadership(NAMESPACE, "test-holder", now + timedelta(seconds=302))

    forbidden = [
        ("PATCH", resource_path("Secret", NAMESPACE, "credentials")),
        ("PATCH", resource_path("ServiceAccount", NAMESPACE, "identity")),
        ("POST", f"/apis/rbac.authorization.k8s.io/v1/namespaces/{NAMESPACE}/roles"),
        ("GET", resource_path("Secret", "default", "outside")),
        ("PATCH", "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/other.example.com"),
    ]
    for method, path in forbidden:
        with pytest.raises(KubeError) as denied:
            contender.request(method, path, json={}, headers={"Content-Type": "application/merge-patch+json"})
        assert denied.value.status == 403, path
    crd = "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/pigdeployments.governance.promptless.ai"
    assert contender.request("GET", crd)["spec"]["scope"] == "Namespaced"
    patched = contender.request(
        "PATCH",
        crd,
        json={"metadata": {"annotations": {"integration": "allowed"}}},
        headers={"Content-Type": "application/merge-patch+json"},
    )
    assert patched["metadata"]["annotations"]["integration"] == "allowed"
