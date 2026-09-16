"""Exercise Kubernetes request failures and status deletion semantics."""

import json
from time import monotonic

import httpx
import pytest
from pig_supervisor.kube import Kube, KubeError


def test_status_patch_removes_completed_transition_fields() -> None:
    deployment = {
        "metadata": {"namespace": "pig", "name": "worker", "resourceVersion": "17"},
        "status": {
            "phase": "preflight",
            "retryAt": "2026-09-16T00:00:00Z",
            "acceptanceNextCheck": "2026-09-16T00:00:00Z",
            "attempt": 1,
        },
    }
    desired = {"phase": "quiesce", "attempt": 0}
    persisted = dict(deployment["status"])

    def respond(request: httpx.Request) -> httpx.Response:
        patch = json.loads(request.content)
        assert request.method == "PATCH"
        assert request.url.path.endswith("/pigdeployments/worker/status")
        assert request.headers["Content-Type"] == "application/merge-patch+json"
        assert patch["metadata"]["resourceVersion"] == "17"
        assert patch["status"]["retryAt"] is None
        assert patch["status"]["acceptanceNextCheck"] is None
        for key, value in patch["status"].items():
            if value is None:
                persisted.pop(key, None)
            else:
                persisted[key] = value
        return httpx.Response(200, json={"status": persisted})

    with httpx.Client(base_url="https://kubernetes.example", transport=httpx.MockTransport(respond)) as client:
        kube = Kube(client)
        kube.lease_deadline = monotonic() + 30
        kube.status(deployment, desired)

    assert persisted == desired
    assert deployment["status"]["retryAt"] == "2026-09-16T00:00:00Z"


def test_failed_request_exposes_operation_without_response_contents() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "private-install-token"})

    with (
        httpx.Client(base_url="https://kubernetes.example", transport=httpx.MockTransport(respond)) as client,
        pytest.raises(KubeError) as caught,
    ):
        Kube(client).get("Secret", "pig", "credentials")

    assert caught.value.status == 403
    assert caught.value.method == "GET"
    assert caught.value.path == "/api/v1/namespaces/pig/secrets/credentials"
    assert "private-install-token" not in str(caught.value)


def test_expired_lease_explains_rejected_write() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail("an expired lease must prevent sending the write")

    with (
        httpx.Client(base_url="https://kubernetes.example", transport=httpx.MockTransport(respond)) as client,
        pytest.raises(KubeError) as caught,
    ):
        Kube(client).patch("Deployment", "pig", "worker", {"spec": {"replicas": 0}})

    assert caught.value.status == 409
    assert caught.value.method == "PATCH"
    assert caught.value.path == "/apis/apps/v1/namespaces/pig/deployments/worker"
    assert caught.value.reason == "controller Lease expired"
