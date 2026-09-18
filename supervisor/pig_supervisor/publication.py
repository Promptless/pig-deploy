"""Assemble reviewed release artifacts; publishing is an explicit operator workflow."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import yaml
from pydantic import Field, model_validator

from .catalog import canonical_digest
from .models import Contract, Digest, Image, Release, Requirements, stable_version

REPO = "https://github.com/Promptless/pig-deploy"
CHARTS = ("pig-supervisor", "pig-trace-analyzer")
CHECKS = frozenset(
    {
        "install",
        "canonicalAcceptance",
        "minorUpdate",
        "majorUpdate",
        "pausePin",
        "blockedResume",
        "secretRotation",
        "supervisorSelfUpdate",
        "recovery",
    }
)


class CloudAcceptance(Contract):
    tested_at: datetime
    evidence: dict[str, str]

    @model_validator(mode="after")
    def complete(self):
        if set(self.evidence) != CHECKS:
            raise ValueError("cloud acceptance must include every required check")
        if not all(re.fullmatch(r"https://[^\s]+", value) for value in self.evidence.values()):
            raise ValueError("every acceptance check needs a public sanitized evidence URL")
        if self.tested_at.tzinfo is None:
            raise ValueError("testedAt requires a timezone")
        return self


class AcceptanceEvidence(Contract):
    version: str
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    analyzer_image: Image
    supervisor_image: Image
    requirements_digest: Digest
    rollback_to: list[Digest] = Field(default_factory=list)
    eks: CloudAcceptance
    aks: CloudAcceptance
    gke: CloudAcceptance


def validate_evidence(data: dict, version: str, now: datetime) -> AcceptanceEvidence:
    """Require recent, exact-artifact evidence for all three real cloud environments."""
    stable_version(version)
    evidence = AcceptanceEvidence.model_validate(data)
    if evidence.version != version:
        raise ValueError("evidence version differs from the requested release")
    if not evidence.analyzer_image.startswith("ghcr.io/promptless/pig-trace-analyzer@"):
        raise ValueError("analyzer must use the worker image repository")
    if not evidence.supervisor_image.startswith("ghcr.io/promptless/pig-supervisor@"):
        raise ValueError("supervisor must use its own image repository")
    for cloud in (evidence.eks, evidence.aks, evidence.gke):
        if not now - timedelta(days=14) <= cloud.tested_at <= now:
            raise ValueError("acceptance evidence must be from the last fourteen days")
    return evidence


def check_capabilities(capabilities: dict, requirements: Requirements) -> None:
    """A stale worker image cannot be packaged merely by changing a chart version."""
    if (
        capabilities.get("controllerProtocol") != requirements.controller_protocol
        or capabilities.get("schemaRevision") != requirements.schema_to
        or not set(requirements.storage_backends) <= set(capabilities.get("storageBackends", []))
        or not {"preflight", "supervised-migrate", "verify", "acceptance"} <= set(capabilities.get("commands", []))
        or not {"native-storage-v1", "migration-ledger-v1"} <= set(capabilities.get("capabilities", []))
    ):
        raise ValueError("worker image does not implement this deployment contract")


class Registry:
    """Read exact GHCR manifests with either fresh anonymous or explicit CI authentication."""

    def __init__(self, client: httpx.Client, username: str | None = None, password: str | None = None):
        self.client = client
        self.auth = (username, password) if username and password else None

    def manifest(self, name: str, reference: str, *, missing_ok: bool = False) -> tuple[str, dict] | None:
        if not re.fullmatch(r"(charts/)?(pig-supervisor|pig-trace-analyzer)", name):
            raise ValueError("unexpected registry repository")
        if not re.fullmatch(r"(sha256:[a-f0-9]{64}|[0-9]+\.[0-9]+\.[0-9]+)", reference):
            raise ValueError("expected an immutable digest or canonical release version")
        response = self.client.get(
            "https://ghcr.io/token",
            params={"service": "ghcr.io", "scope": f"repository:promptless/{name}:pull"},
            auth=self.auth,
        )
        response.raise_for_status()
        token = response.json()["token"]
        response = self.client.get(
            f"https://ghcr.io/v2/promptless/{name}/manifests/{reference}",
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json",
            },
        )
        if response.status_code == 404 and missing_ok:
            return None
        response.raise_for_status()
        digest = "sha256:" + sha256(response.content).hexdigest()
        if reference.startswith("sha256:") and digest != reference:
            raise ValueError("registry response digest differs from the pinned image")
        if response.headers.get("Docker-Content-Digest") != digest:
            raise ValueError("registry digest header does not match manifest bytes")
        return digest, response.json()


def reproducible_chart(path: Path) -> None:
    """Remove packaging timestamps so an interrupted publication can resume without changing bytes."""
    result = io.BytesIO()
    with (
        tarfile.open(path, "r:gz") as source,
        gzip.GzipFile(fileobj=result, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as target,
    ):
        for member in sorted(source.getmembers(), key=lambda item: item.name):
            if not member.isfile():
                raise ValueError("chart packages must contain only regular files")
            member.mtime = member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.mode = 0o644
            target.addfile(member, source.extractfile(member))
    path.write_bytes(result.getvalue())


def chart_publication_needed(registry: Registry, output: Path, version: str) -> dict[str, bool]:
    """Allow only absent versions or exact accepted chart bytes; never overwrite a version."""
    needed = {}
    for chart in CHARTS:
        result = registry.manifest("charts/" + chart, version, missing_ok=True)
        needed[chart] = result is None
        if result is not None:
            package_hash = "sha256:" + sha256((output / f"{chart}-{version}.tgz").read_bytes()).hexdigest()
            layers = [
                layer
                for layer in result[1].get("layers", [])
                if layer.get("mediaType") == "application/vnd.cncf.helm.chart.content.v1.tar+gzip"
            ]
            if len(layers) != 1 or layers[0].get("digest") != package_hash:
                raise ValueError("chart version already exists with different bytes; it cannot be overwritten")
    return needed


def package(root: Path, output: Path, evidence: AcceptanceEvidence, requirements: Requirements) -> dict:
    """Package only versioned public deployment files, with exact image digests inserted."""
    if canonical_digest(requirements.model_dump(by_alias=True)) != evidence.requirements_digest:
        raise ValueError("acceptance does not cover the exact release requirements")
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if source != evidence.source_commit:
        raise ValueError("checkout does not match accepted deployment source")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, text=True).strip():
        raise ValueError("release source must have no tracked modifications")
    output.mkdir(parents=True, exist_ok=True)
    hashes = {}
    with tempfile.TemporaryDirectory() as directory:
        for chart, image in zip(CHARTS, (evidence.supervisor_image, evidence.analyzer_image), strict=True):
            target = Path(directory) / chart
            # Package only tracked files from the accepted commit, never untracked local files.
            paths = subprocess.check_output(
                ["git", "ls-tree", "-r", "--name-only", evidence.source_commit, "charts/" + chart],
                cwd=root,
                text=True,
            ).splitlines()
            for path in paths:
                destination = target / Path(path).relative_to("charts/" + chart)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(
                    subprocess.check_output(["git", "show", evidence.source_commit + ":" + path], cwd=root)
                )
            metadata = yaml.safe_load((target / "Chart.yaml").read_text())
            if metadata["version"] != evidence.version or metadata["appVersion"] != evidence.version:
                raise ValueError("chart source versions must match the accepted release")
            values = yaml.safe_load((target / "values.yaml").read_text())
            values["image"]["repository"], values["image"]["digest"] = image.split("@")
            (target / "values.yaml").write_text(yaml.safe_dump(values, sort_keys=False))
            subprocess.run(["helm", "package", str(target), "--destination", str(output)], check=True)
            filename = f"{chart}-{evidence.version}.tgz"
            reproducible_chart(output / filename)
            hashes[filename] = sha256((output / filename).read_bytes()).hexdigest()
    archive = f"pig-deploy-{evidence.version}.tar.gz"
    subprocess.run(
        [
            "git",
            "archive",
            "--format=tar.gz",
            f"--prefix=pig-deploy-{evidence.version}/",
            f"--output={output / archive}",
            evidence.source_commit,
        ],
        cwd=root,
        check=True,
    )
    hashes[archive] = sha256((output / archive).read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text("".join(f"{value}  {name}\n" for name, value in sorted(hashes.items())))
    return hashes


def assemble(
    root: Path, output: Path, evidence: AcceptanceEvidence, requirements: Requirements, registry: Registry
) -> Release:
    """Require anonymous access to both images and the exact packaged OCI charts."""
    for image in (evidence.supervisor_image, evidence.analyzer_image):
        name, digest = image.removeprefix("ghcr.io/promptless/").split("@")
        registry.manifest(name, digest)
    charts = {}
    for chart in CHARTS:
        result = registry.manifest("charts/" + chart, evidence.version)
        assert result is not None
        digest, manifest = result
        package_hash = sha256((output / f"{chart}-{evidence.version}.tgz").read_bytes()).hexdigest()
        layers = [
            layer
            for layer in manifest.get("layers", [])
            if layer.get("mediaType") == "application/vnd.cncf.helm.chart.content.v1.tar+gzip"
        ]
        if len(layers) != 1 or layers[0]["digest"] != "sha256:" + package_hash:
            raise ValueError("anonymous OCI chart does not contain the accepted package")
        charts[chart] = {
            "repository": "oci://ghcr.io/promptless/charts/" + chart,
            "version": evidence.version,
            "sha256": package_hash,
            "ociDigest": digest,
        }
    crd = root / "charts/pig-supervisor/crds/pigdeployments.yaml"
    return Release.model_validate(
        {
            "version": evidence.version,
            "analyzerImage": evidence.analyzer_image,
            "supervisorImage": evidence.supervisor_image,
            "requirements": requirements.model_dump(by_alias=True),
            "rollbackTo": evidence.rollback_to,
            "crd": {
                "url": f"https://raw.githubusercontent.com/Promptless/pig-deploy/{evidence.source_commit}/charts/pig-supervisor/crds/pigdeployments.yaml",
                "sha256": sha256(crd.read_bytes()).hexdigest(),
            },
            "artifacts": {
                "sourceCommit": evidence.source_commit,
                "terraformArchiveURL": f"{REPO}/releases/download/v{evidence.version}/pig-deploy-{evidence.version}.tar.gz",
                "terraformArchiveSha256": sha256(
                    (output / f"pig-deploy-{evidence.version}.tar.gz").read_bytes()
                ).hexdigest(),
                "supervisorChart": charts["pig-supervisor"],
                "workerChart": charts["pig-trace-analyzer"],
            },
        }
    )


def promote_catalog(root: Path, manifest: Path, commit: str) -> None:
    """Add one immutable manifest; never replace an existing stable version."""
    release = Release.model_validate_json(manifest.read_text())
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("manifest must be addressed by an exact Git commit")
    if release.artifacts is None:
        raise ValueError("published release is missing its artifact inventory")
    path = root / "catalog/stable.json"
    catalog = json.loads(path.read_text())
    if any(entry["version"] == release.version for entry in catalog["releases"]):
        raise ValueError("a published version cannot be replaced")
    catalog["releases"].append(
        {
            "version": release.version,
            "url": f"https://raw.githubusercontent.com/Promptless/pig-deploy/{commit}/catalog/releases/{release.version}.json",
            "sha256": sha256(manifest.read_bytes()).hexdigest(),
        }
    )
    catalog["releases"].sort(key=lambda entry: stable_version(entry["version"]))
    path.write_text(json.dumps(catalog, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=["evidence", "package", "chart-status", "assemble", "promote", "promote-draft"]
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".release"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-commit")
    parser.add_argument("--capabilities", type=Path)
    args = parser.parse_args()
    stable_version(args.version)
    if args.operation in {"promote", "promote-draft"}:
        if Release.model_validate_json(args.manifest.read_text()).version != args.version:
            raise ValueError("manifest version differs from the requested catalog promotion")
        if args.operation == "promote-draft":
            from .promotion import promote_draft

            print(promote_draft(args.root, args.manifest))
        else:
            promote_catalog(args.root, args.manifest, args.manifest_commit)
        return
    evidence = validate_evidence(json.loads(args.evidence.read_text()), args.version, datetime.now(UTC))
    if args.operation == "evidence":
        if output := os.environ.get("GITHUB_OUTPUT"):
            with Path(output).open("a") as handle:
                handle.writelines(
                    f"{name}={getattr(evidence, name)}\n"
                    for name in ("source_commit", "analyzer_image", "supervisor_image")
                )
        print(json.dumps(evidence.model_dump(mode="json", by_alias=True), indent=2))
        return
    requirements = Requirements.model_validate_json(args.requirements.read_text())
    if args.operation == "package":
        check_capabilities(json.loads(args.capabilities.read_text()), requirements)
        package(args.root, args.output.resolve(), evidence, requirements)
        return
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        if args.operation == "chart-status":
            registry = Registry(client, os.environ.get("GITHUB_ACTOR"), os.environ.get("GH_TOKEN"))
            needed = chart_publication_needed(registry, args.output, args.version)
            if output := os.environ.get("GITHUB_OUTPUT"):
                with Path(output).open("a") as handle:
                    handle.writelines(f"{name}={str(value).lower()}\n" for name, value in needed.items())
            print(json.dumps(needed))
        else:
            release = assemble(args.root, args.output, evidence, requirements, Registry(client))
            (args.output / "release.json").write_text(release.model_dump_json(by_alias=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
