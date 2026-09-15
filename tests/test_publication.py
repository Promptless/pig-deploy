"""Exercise release gates without registry publication or cloud access."""

import json
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from pig_supervisor.catalog import canonical_digest
from pig_supervisor.models import Requirements
from pig_supervisor.publication import (
    CHECKS,
    Registry,
    assemble,
    chart_publication_needed,
    check_capabilities,
    package,
    promote_catalog,
    validate_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 15, tzinfo=UTC)


def evidence_data(commit="a" * 40):
    requirements = Requirements.model_validate_json((ROOT / "releases/requirements/0.3.0.json").read_text())
    data = {
        "version": "0.3.0",
        "sourceCommit": commit,
        "analyzerImage": "ghcr.io/promptless/instruction-hub-worker@sha256:" + "a" * 64,
        "supervisorImage": "ghcr.io/promptless/pig-supervisor@sha256:" + "b" * 64,
        "requirementsDigest": canonical_digest(requirements.model_dump(by_alias=True)),
        **{
            cloud: {
                "testedAt": NOW.isoformat(),
                "evidence": {check: "https://example.com/acceptance/" + check for check in CHECKS},
            }
            for cloud in ("eks", "aks", "gke")
        },
    }
    return data, requirements


def test_evidence_requires_real_cloud_checks_and_exact_artifacts():
    data, _ = evidence_data()
    assert validate_evidence(data, "0.3.0", NOW).version == "0.3.0"
    with pytest.raises(ValueError):
        validate_evidence(data, "0.3.1", NOW)
    with pytest.raises(ValueError):
        validate_evidence(data, "0.3.0", NOW + timedelta(days=15))
    del data["gke"]["evidence"]["recovery"]
    with pytest.raises(ValueError):
        validate_evidence(data, "0.3.0", NOW)


def test_stale_worker_image_cannot_be_relabelled_as_native_release():
    _, requirements = evidence_data()
    with pytest.raises(ValueError, match="worker image"):
        check_capabilities({"storageBackends": ["s3"]}, requirements)
    check_capabilities(
        {
            "controllerProtocol": 1,
            "schemaRevision": 1,
            "storageBackends": ["s3", "azureBlob", "gcs"],
            "commands": ["preflight", "supervised-migrate", "verify", "acceptance"],
            "capabilities": ["native-storage-v1", "migration-ledger-v1"],
        },
        requirements,
    )


def test_registry_checks_anonymous_access_and_content_digest():
    payload = b'{"schemaVersion":2}'
    digest = "sha256:" + sha256(payload).hexdigest()

    def respond(request):
        if request.url.path == "/token":
            assert "Authorization" not in request.headers
            return httpx.Response(200, json={"token": "anonymous"})
        assert request.headers["Authorization"] == "Bearer anonymous"
        return httpx.Response(200, content=payload, headers={"Docker-Content-Digest": digest})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        registry = Registry(client)
        assert registry.manifest("pig-supervisor", digest)[0] == digest
        with pytest.raises(ValueError, match="differs"):
            registry.manifest("pig-supervisor", "sha256:" + "f" * 64)


def test_real_helm_package_immutable_manifest_and_catalog(tmp_path):
    root, output = tmp_path / "source", tmp_path / "packages"
    root.mkdir()
    shutil.copytree(ROOT / "charts", root / "charts")
    (root / "catalog").mkdir()
    (root / "catalog/stable.json").write_text('{"schemaVersion":1,"releases":[]}')
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "charts", "catalog"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Release test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
        cwd=root,
        check=True,
    )
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    data, requirements = evidence_data(commit)
    evidence = validate_evidence(data, "0.3.0", NOW)
    hashes = package(root, output, evidence, requirements)
    assert set(hashes) == {"pig-supervisor-0.3.0.tgz", "instruction-hub-worker-0.3.0.tgz", "pig-deploy-0.3.0.tar.gz"}
    (root / "charts/pig-supervisor/untracked-secret").write_text("must never be packaged")
    assert package(root, tmp_path / "repeated", evidence, requirements) == hashes
    with tarfile.open(output / "pig-supervisor-0.3.0.tgz") as archive:
        assert not any("untracked-secret" in entry.name for entry in archive)

    class Published:
        def manifest(self, name, ref, **kwargs):
            if name.startswith("charts/"):
                filename = name.removeprefix("charts/") + "-0.3.0.tgz"
                return "sha256:" + "c" * 64, {
                    "layers": [
                        {
                            "mediaType": "application/vnd.cncf.helm.chart.content.v1.tar+gzip",
                            "digest": "sha256:" + hashes[filename],
                        }
                    ]
                }
            return ref, {}

    release = assemble(root, output, evidence, requirements, Published())
    assert chart_publication_needed(Published(), output, "0.3.0") == dict.fromkeys(
        ("pig-supervisor", "instruction-hub-worker"), False
    )
    (output / "pig-supervisor-0.3.0.tgz").write_bytes(b"different package")
    with pytest.raises(ValueError, match="cannot be overwritten"):
        chart_publication_needed(Published(), output, "0.3.0")
    path = root / "catalog/releases/0.3.0.json"
    path.parent.mkdir()
    path.write_text(release.model_dump_json(by_alias=True, indent=2) + "\n")
    promote_catalog(root, path, "d" * 40)
    catalog = json.loads((root / "catalog/stable.json").read_text())
    assert catalog["releases"][0]["sha256"] == sha256(path.read_bytes()).hexdigest()
    assert release.artifacts.source_commit == commit
    with pytest.raises(ValueError, match="cannot be replaced"):
        promote_catalog(root, path, "e" * 40)
