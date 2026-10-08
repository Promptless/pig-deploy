"""Release inspection and publication still share the Python manifest contract."""

import json
from hashlib import sha256

import httpx
import pytest
from pig_supervisor.catalog import CatalogError, select_release
from pig_supervisor.models import Release
from pydantic import ValidationError

ROOT = "https://raw.githubusercontent.com/Promptless/pig-deploy/" + "e" * 40
CATALOG = "https://raw.githubusercontent.com/Promptless/pig-deploy/main/catalog/stable.json"


def manifest(version="1.0.0", **requirements):
    return {
        "version": version,
        "analyzerImage": "ghcr.io/promptless/pig-trace-analyzer@sha256:" + "a" * 64,
        "supervisorImage": "ghcr.io/promptless/pig-supervisor@sha256:" + "b" * 64,
        "requirements": {
            "storageBackends": ["s3", "azureBlob", "gcs"],
            "schemaFrom": [0, 1],
            "schemaTo": 1,
            **requirements,
        },
    }


def client_for(*documents):
    bodies = {}
    entries = []
    for document in documents:
        body = json.dumps(document).encode()
        url = ROOT + "/catalog/releases/" + document["version"] + ".json"
        entries.append({"version": document["version"], "url": url, "sha256": sha256(body).hexdigest()})
        bodies[url] = body
    bodies[CATALOG] = json.dumps({"schemaVersion": 1, "releases": entries}).encode()
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=bodies[str(request.url)]))
    )


def test_stable_includes_major_and_pin_is_exact():
    with client_for(manifest("1.0.0"), manifest("4.0.0")) as client:
        assert select_release(client, CATALOG).release.version == "4.0.0"
        assert select_release(client, CATALOG, "1.0.0").release.version == "1.0.0"
        with pytest.raises(CatalogError):
            select_release(client, CATALOG, "2.0.0")


def test_catalog_detects_tampered_manifest():
    with client_for(manifest()) as client:
        original = client._transport.handler
        client._transport.handler = lambda req: (
            httpx.Response(200, content=b"{}") if str(req.url) != CATALOG else original(req)
        )
        with pytest.raises(CatalogError, match="checksum"):
            select_release(client, CATALOG)


@pytest.mark.parametrize("version", ["1.0.0rc1", "1.0.0+local", "1.0", "v1.0.0"])
def test_nonstable_versions_cannot_enter_the_catalog(version):
    with pytest.raises(ValidationError):
        Release.model_validate(manifest(version))


def test_destructive_release_cannot_omit_recovery_prerequisite():
    with pytest.raises(ValidationError):
        Release.model_validate(manifest(destructiveMigration=True))
