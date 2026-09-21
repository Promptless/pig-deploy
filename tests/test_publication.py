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
    INSTALL_CHECKS,
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
        "analyzerImage": "ghcr.io/promptless/pig-trace-analyzer@sha256:" + "a" * 64,
        "supervisorImage": "ghcr.io/promptless/pig-supervisor@sha256:" + "b" * 64,
        "requirementsDigest": canonical_digest(requirements.model_dump(by_alias=True)),
        **{
            cloud: {
                "testedAt": NOW.isoformat(),
                "evidence": {check: "https://example.com/acceptance/" + check for check in INSTALL_CHECKS},
            }
            for cloud in ("eks",)
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


@pytest.mark.parametrize("check", sorted(INSTALL_CHECKS))
def test_initial_release_requires_both_install_and_canonical_acceptance(check):
    data, _ = evidence_data()
    del data["eks"]["evidence"][check]
    with pytest.raises(ValueError, match="required checks"):
        validate_evidence(data, "0.3.0", NOW)


def test_initial_release_cannot_omit_aws_or_advertise_untested_rollback():
    data, _ = evidence_data()
    aws = data.pop("eks")
    with pytest.raises(ValueError):
        validate_evidence(data, "0.3.0", NOW)
    data["eks"] = aws
    data["rollbackTo"] = ["c" * 64]
    with pytest.raises(ValueError, match="recovery acceptance"):
        validate_evidence(data, "0.3.0", NOW)
    aws["evidence"]["recovery"] = "https://example.com/acceptance/recovery"
    assert validate_evidence(data, "0.3.0", NOW).rollback_to == ["c" * 64]


@pytest.mark.parametrize("version", ["0.3.1", "1.0.0"])
def test_later_releases_retain_full_three_cloud_lifecycle_gate(version):
    data, _ = evidence_data()
    data["version"] = version
    with pytest.raises(ValueError):
        validate_evidence(data, version, NOW)
    for cloud in ("eks", "aks", "gke"):
        data[cloud] = {
            "testedAt": NOW.isoformat(),
            "evidence": {check: "https://example.com/acceptance/" + check for check in CHECKS},
        }
    assert validate_evidence(data, version, NOW).version == version
    for cloud in ("aks", "gke"):
        report = data.pop(cloud)
        with pytest.raises(ValueError, match=f"{cloud} acceptance is required"):
            validate_evidence(data, version, NOW)
        data[cloud] = None
        with pytest.raises(ValueError, match=f"{cloud} acceptance is required"):
            validate_evidence(data, version, NOW)
        data[cloud] = report
    del data["gke"]["evidence"]["recovery"]
    with pytest.raises(ValueError, match="required checks"):
        validate_evidence(data, version, NOW)


@pytest.mark.parametrize("cloud", ["aks", "gke"])
def test_optional_experimental_cloud_reports_still_require_fresh_complete_evidence(cloud):
    data, _ = evidence_data()
    data[cloud] = {"testedAt": (NOW - timedelta(days=15)).isoformat(), "evidence": dict(data["eks"]["evidence"])}
    with pytest.raises(ValueError, match="fourteen days"):
        validate_evidence(data, "0.3.0", NOW)
    data[cloud]["testedAt"] = NOW.isoformat()
    assert getattr(validate_evidence(data, "0.3.0", NOW), cloud) is not None
    del data[cloud]["evidence"]["canonicalAcceptance"]
    with pytest.raises(ValueError, match="required checks"):
        validate_evidence(data, "0.3.0", NOW)


@pytest.mark.parametrize("link", ["http://example.com/report", "https://example.com/private report"])
def test_acceptance_reports_require_https_links(link):
    data, _ = evidence_data()
    data["eks"]["evidence"]["install"] = link
    with pytest.raises(ValueError, match="public sanitized evidence URL"):
        validate_evidence(data, "0.3.0", NOW)


def test_stale_worker_image_cannot_be_relabelled_as_native_release():
    _, requirements = evidence_data()
    with pytest.raises(ValueError, match="worker image"):
        check_capabilities({"storageBackends": ["s3"]}, requirements)


@pytest.mark.parametrize("schema_revision", [1, 2, 3, 4])
@pytest.mark.parametrize("installation_identity", [False, True])
def test_candidate_requires_schema_3_worker_and_credential_identity(schema_revision, installation_identity):
    _, requirements = evidence_data()
    capabilities = {
        "controllerProtocol": 1,
        "schemaRevision": schema_revision,
        "storageBackends": ["s3", "azureBlob", "gcs"],
        "commands": ["preflight", "supervised-migrate", "verify", "acceptance"],
        "capabilities": ["native-storage-v1", "migration-ledger-v1"],
    }
    if installation_identity:
        capabilities["capabilities"].append("installation-identity-v1")
    if schema_revision != 3 or not installation_identity:
        with pytest.raises(ValueError, match="worker image"):
            check_capabilities(capabilities, requirements)
    else:
        check_capabilities(capabilities, requirements)


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
    assert set(hashes) == {"pig-supervisor-0.3.0.tgz", "pig-trace-analyzer-0.3.0.tgz", "pig-deploy-0.3.0.tar.gz"}
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
        ("pig-supervisor", "pig-trace-analyzer"), False
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
