"""Exercise Kubernetes request failures and status deletion semantics."""

import json
from datetime import UTC, datetime, timedelta
from time import monotonic

import httpx
import pytest
from pig_supervisor.kube import Kube, KubeError, resource_path


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


@pytest.mark.parametrize(
    "kind,namespace,name",
    [
        ("Deployment", "pig", "worker"),
        ("Deployment", "leases-prod", "worker"),
        ("Deployment", "pig", "leases-analyzer"),
        ("PIGDeployment", "pig", "leases"),
    ],
)
def test_expired_lease_explains_rejected_write(kind, namespace, name) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail("an expired lease must prevent sending the write")

    with (
        httpx.Client(base_url="https://kubernetes.example", transport=httpx.MockTransport(respond)) as client,
        pytest.raises(KubeError) as caught,
    ):
        Kube(client).patch(kind, namespace, name, {"spec": {"replicas": 0}})

    assert caught.value.status == 409
    assert caught.value.method == "PATCH"
    assert caught.value.path == resource_path(kind, namespace, name)
    assert caught.value.reason == "controller Lease expired"


@pytest.fixture
def lease_api(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("pig_supervisor.kube.monotonic", lambda: clock[0])
    lease = None
    writes = []

    def respond(request):
        nonlocal lease
        if request.url.path in (resource_path("Lease", "pig"), resource_path("Lease", "pig", "pig-supervisor")):
            if request.method == "GET":
                return httpx.Response(200, json=lease) if lease else httpx.Response(404, json={})
            incoming = json.loads(request.content)
            if lease and (
                request.method == "POST"
                or incoming["metadata"].get("resourceVersion") != lease["metadata"]["resourceVersion"]
            ):
                return httpx.Response(409, json={})
            revision = int(lease["metadata"]["resourceVersion"]) + 1 if lease else 1
            incoming["metadata"].update(uid="lease-uid", resourceVersion=str(revision))
            lease = incoming
            return httpx.Response(200, json=lease)
        writes.append(request.url.path)
        return httpx.Response(200, json={})

    with httpx.Client(base_url="https://kubernetes.example", transport=httpx.MockTransport(respond)) as client:
        yield client, clock, writes


@pytest.mark.parametrize("offset", [-3600, 0, 60, 86400])
def test_takeover_waits_for_locally_observed_expiry_despite_clock_offset(lease_api, offset):
    client, clock, writes = lease_api
    leader, contender = Kube(client), Kube(client)
    now = datetime(2026, 9, 16, tzinfo=UTC)
    assert leader.leadership("pig", "first", now)
    assert not contender.leadership("pig", "second", now + timedelta(seconds=offset))
    clock[0] += 241
    assert not contender.leadership("pig", "second", now + timedelta(seconds=241 + offset))
    leader.patch("Deployment", "pig", "worker", {"spec": {"replicas": 0}})
    with pytest.raises(KubeError, match="Lease expired"):
        contender.patch("Deployment", "pig", "worker", {"spec": {"replicas": 1}})
    clock[0] += 59
    assert contender.leadership("pig", "second", now + timedelta(seconds=300 + offset))
    with pytest.raises(KubeError, match="Lease expired"):
        leader.patch("Deployment", "pig", "worker", {"spec": {"replicas": 0}})
    contender.patch("Deployment", "pig", "worker", {"spec": {"replicas": 1}})
    assert len(writes) == 2


def test_observed_renewal_and_process_restart_each_restart_takeover_wait(lease_api):
    client, clock, _ = lease_api
    leader, contender = Kube(client), Kube(client)
    now = datetime(2026, 9, 16, tzinfo=UTC)
    assert leader.leadership("pig", "first", now)
    assert not contender.leadership("pig", "second", now)
    clock[0] += 240
    assert leader.leadership("pig", "first", now + timedelta(seconds=240))
    assert not contender.leadership("pig", "second", now + timedelta(seconds=240))
    clock[0] += 60
    assert not contender.leadership("pig", "second", now + timedelta(seconds=300))
    clock[0] += 240
    restarted = Kube(client)
    assert not restarted.leadership("pig", "third", now + timedelta(days=10))
    assert contender.leadership("pig", "second", now + timedelta(seconds=540))
    assert not restarted.leadership("pig", "third", now + timedelta(days=10))


@pytest.mark.parametrize("existing", [False, True])
def test_lease_conflict_never_grants_write_authority(monkeypatch, existing):
    clock = [1000.0]
    monkeypatch.setattr("pig_supervisor.kube.monotonic", lambda: clock[0])
    now = datetime(2026, 9, 16, tzinfo=UTC)

    def respond(request):
        if request.method == "GET":
            return httpx.Response(
                200 if existing else 404,
                json={
                    "metadata": {"resourceVersion": "1"},
                    "spec": {"holderIdentity": "other", "leaseDurationSeconds": 300, "renewTime": now.isoformat()},
                },
            )
        assert request.url.path.startswith("/apis/coordination.k8s.io/v1/")
        return httpx.Response(409, json={})

    with httpx.Client(base_url="https://kubernetes.example", transport=httpx.MockTransport(respond)) as client:
        kube = Kube(client)
        assert not kube.leadership("pig", "contender", now)
        clock[0] += 300
        assert not kube.leadership("pig", "contender", now + timedelta(seconds=300))
        with pytest.raises(KubeError, match="Lease expired"):
            kube.patch("Deployment", "pig", "worker", {"spec": {"replicas": 1}})
