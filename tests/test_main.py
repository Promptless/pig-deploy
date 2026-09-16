"""Check that failure descriptions preserve diagnosis while excluding credential data."""

import json
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import httpx
import pytest
import yaml
from pig_supervisor.catalog import CatalogError
from pig_supervisor.controller import Controller
from pig_supervisor.kube import Kube
from pig_supervisor.main import describe_failure
from pig_supervisor.models import SecretRef
from pydantic import ValidationError


def test_http_failure_description_omits_credentials_and_response_body() -> None:
    request = httpx.Request("GET", "https://private-user:private-password@example.com/private-path?token=private-token")
    response = httpx.Response(403, request=request, text="private-response-body")
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()

    assert describe_failure(caught.value) == "HTTPStatusError: GET example.com returned 403"
    assert describe_failure(httpx.ConnectTimeout("private-password", request=request)) == (
        "ConnectTimeout: GET example.com"
    )


def test_validation_failure_description_omits_rejected_input() -> None:
    with pytest.raises(ValidationError) as caught:
        SecretRef.model_validate({"name": "private-password!", "key": "token"})

    assert describe_failure(caught.value) == "ValidationError: name: string_pattern_mismatch"


def test_catalog_failure_description_retains_integrity_failure() -> None:
    assert describe_failure(CatalogError("release artifact checksum mismatch")) == (
        "CatalogError: release artifact checksum mismatch"
    )


def test_controller_persists_safe_validation_details() -> None:
    """Invalid configuration reports its field and type through the real status request."""
    document = yaml.safe_load((Path(__file__).resolve().parents[1] / "examples/pig-deployment.yaml").read_text())
    document["metadata"].update(name="acme", namespace="pig", uid="example-uid", generation=1, resourceVersion="1")
    document["spec"]["serviceAccountName"] = "private-install-token!"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path.endswith("/pigdeployments/acme/status")
        status = json.loads(request.content)["status"]
        requests.append(request)
        return httpx.Response(200, json={"status": status})

    with httpx.Client(base_url="https://kubernetes.example", transport=httpx.MockTransport(respond)) as client:
        kube = Kube(client)
        kube.lease_deadline = monotonic() + 30
        controller = Controller(kube, client, "https://catalog.example", "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 1, datetime.now(UTC))

    assert len(requests) == 1
    conditions = json.loads(requests[0].content)["status"]["conditions"]
    blocked = next(condition for condition in conditions if condition["type"] == "Blocked")
    ready = next(condition for condition in conditions if condition["type"] == "Ready")
    assert blocked["status"] == "True"
    assert blocked["reason"] == "InvalidConfiguration"
    assert "serviceAccountName: string_pattern_mismatch" in blocked["message"]
    assert ready["status"] == "False"
    assert ready["message"] == blocked["message"]
    assert "private-install-token" not in json.dumps(conditions)
