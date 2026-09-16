"""Digest-verified release selection, with TLS as the catalog trust root."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import httpx
from pydantic import Field

from .models import Artifact, Contract, Release, stable_version

MAX_CATALOG_BYTES = 1024 * 1024


class CatalogError(RuntimeError):
    """The release catalog is unavailable, inconsistent, or outside the trust boundary."""


class Entry(Artifact):
    version: str


class Catalog(Contract):
    schema_version: int = Field(ge=1, le=1)
    releases: list[Entry]


@dataclass(frozen=True)
class SelectedRelease:
    release: Release
    digest: str


def verified_bytes(client: httpx.Client, artifact: Artifact) -> bytes:
    """Reject redirects, unbounded files, and checksum mismatches before parsing."""
    with client.stream("GET", artifact.url) as response:
        response.raise_for_status()
        result = bytearray()
        for chunk in response.iter_bytes():
            result.extend(chunk)
            if len(result) > MAX_CATALOG_BYTES:
                raise CatalogError("release artifact exceeds size limit")
    if hashlib.sha256(result).hexdigest() != artifact.sha256:
        raise CatalogError("release artifact checksum mismatch")
    return bytes(result)


def select_release(client: httpx.Client, catalog_url: str, pinned: str = "") -> SelectedRelease:
    """Resolve an exact pin or the highest stable version without a major-version ceiling."""
    # URL is trusted bootstrap configuration, never an arbitrary PIGDeployment field.
    with client.stream("GET", catalog_url) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > MAX_CATALOG_BYTES:
                raise CatalogError("catalog exceeds size limit")
    catalog = Catalog.model_validate_json(data)
    versions = [stable_version(entry.version) for entry in catalog.releases]
    if len(versions) != len(set(versions)):
        raise CatalogError("catalog contains duplicate immutable versions")
    candidates = [entry for entry in catalog.releases if not pinned or entry.version == pinned]
    if not candidates:
        raise CatalogError("no published release matches this policy")
    entry = max(candidates, key=lambda item: stable_version(item.version))
    release = Release.model_validate_json(verified_bytes(client, entry))
    if release.version != entry.version:
        raise CatalogError("manifest version differs from catalog")
    return SelectedRelease(release, entry.sha256)


def canonical_digest(value: object) -> str:
    """Hash deterministic JSON used for workload settings and secret revisions."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
