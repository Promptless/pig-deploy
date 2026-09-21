"""Versioned deployment and release contracts shared by validation and reconciliation."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic.alias_generators import to_camel

Name = Annotated[str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Image = Annotated[str, Field(pattern=r"^ghcr\.io/promptless/[a-z0-9-]+@sha256:[a-f0-9]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


def validation_details(error: ValidationError) -> str:
    """Identify invalid fields and error types without exposing rejected input or validator context."""
    return ", ".join(
        f"{'.'.join(map(str, item['loc'])) or '<root>'}: {item['type']}"
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    )


class SecretRef(Contract):
    name: Name
    key: Annotated[str, Field(min_length=1, pattern=r"^[a-zA-Z0-9._-]+$")]


class Confirmation(Contract):
    config_map_ref: Name


class ReleasePolicy(Contract):
    channel: Literal["stable"] = "stable"
    paused: bool = False
    pinned_version: str = ""
    confirmation: Confirmation | None = None

    @field_validator("pinned_version")
    @classmethod
    def version(cls, value: str) -> str:
        if value:
            stable_version(value)
        return value


class Hosted(Contract):
    runtime_url: str = Field(default="https://api.gopromptless.ai", alias="runtimeURL")
    install_token_secret_ref: SecretRef

    @field_validator("runtime_url")
    @classmethod
    def origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("hosted runtime requires a credential-free HTTPS URL")
        return value.rstrip("/")


class Endpoint(Contract):
    hostname: str = Field(pattern=r"^[a-zA-Z0-9.-]+$")
    ingress_class_name: str
    tls_secret_name: Name | None = None
    ingress_annotations: dict[str, str] = Field(default_factory=dict)


class Postgres(Contract):
    dsn_secret_ref: SecretRef
    ca_config_map_ref: SecretRef | None = None


class S3(Contract):
    region: str
    bucket: str = Field(min_length=3, pattern=r"^[a-z0-9.-]+$")
    prefix: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9/_.-]+$")


class AzureBlob(Contract):
    account_url: str = Field(alias="accountURL", pattern=r"^https://[a-z0-9]+\.blob\.core\.windows\.net/?$")
    container: Name
    prefix: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9/_.-]+$")


class GCS(Contract):
    bucket: str = Field(min_length=3, pattern=r"^[a-z0-9._-]+$")
    prefix: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9/_.-]+$")


class Storage(Contract):
    postgres: Postgres
    s3: S3 | None = None
    azure_blob: AzureBlob | None = None
    gcs: GCS | None = None

    @model_validator(mode="after")
    def one_backend(self) -> Self:
        if sum(value is not None for value in (self.s3, self.azure_blob, self.gcs)) != 1:
            raise ValueError("exactly one of s3, azureBlob, gcs is required")
        return self


class Repository(Contract):
    url: str = Field(pattern=r"^https://github\.com/[^/]+/[^/]+\.git$")
    id: int = Field(gt=0)
    full_name: str = Field(pattern=r"^[^/]+/[^/]+$")
    token_secret_ref: SecretRef | None = None

    @model_validator(mode="after")
    def identity(self) -> Self:
        if self.url.removeprefix("https://github.com/").removesuffix(".git").casefold() != self.full_name.casefold():
            raise ValueError("repository URL must match fullName")
        return self


class Model(Contract):
    provider: Literal["openai", "azure_openai", "aws_bedrock"]
    authentication: Literal["api_key", "aws_sigv4"]
    base_url: str = Field(alias="baseURL")
    name: str
    api_key_secret_ref: SecretRef | None = None

    @model_validator(mode="after")
    def credentials(self) -> Self:
        if self.authentication == "aws_sigv4":
            if self.provider != "aws_bedrock" or self.api_key_secret_ref is not None:
                raise ValueError("aws_sigv4 requires aws_bedrock and ambient identity without an API key")
        elif self.api_key_secret_ref is None:
            raise ValueError("api_key authentication requires a Secret reference")
        parsed = urlsplit(self.base_url.rstrip("/"))
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("model endpoint must be a credential-free HTTPS URL")
        host = parsed.hostname or ""
        allowed = {
            "openai": host == "api.openai.com" and parsed.path == "/v1",
            "azure_openai": host.endswith((".openai.azure.com", ".services.ai.azure.com"))
            and parsed.path == "/openai/v1",
            "aws_bedrock": re.fullmatch(r"bedrock-mantle\.[a-z0-9-]+\.api\.aws", host) is not None
            and parsed.path in {"/v1", "/openai/v1"},
        }
        if not allowed[self.provider]:
            raise ValueError("model endpoint does not match the supported provider contract")
        return self


class Analysis(Contract):
    activation_at: str = ""
    quiet_window_hours: float = Field(default=0.5, gt=0)
    repository: Repository
    model: Model


class DeploymentSpec(Contract):
    release: ReleasePolicy = Field(default_factory=ReleasePolicy)
    service_account_name: Name
    pod_labels: dict[str, str] = Field(default_factory=dict)
    node_selector: dict[str, str] = Field(default_factory=dict)
    hosted: Hosted
    endpoint: Endpoint
    storage: Storage
    analysis: Analysis


class Requirements(Contract):
    kubernetes_min: str = "1.30.0"
    postgres_min: int = Field(default=15, ge=15)
    postgres_max: int = Field(default=18, ge=15)
    operator_capacity_requirements: list[str] = Field(default_factory=list)
    capacity_confirmation_max_age_hours: int = Field(default=24, gt=0)
    storage_backends: list[Literal["s3", "azureBlob", "gcs"]]
    # RBAC capability names are a finite reviewed contract; a release cannot grant them.
    capabilities: list[str] = Field(default_factory=list)
    recovery_max_age_hours: int | None = Field(default=None, gt=0)
    destructive_migration: bool = False
    schema_from: list[int] = Field(min_length=1)
    schema_to: int = Field(ge=1)
    controller_protocol: Literal[1] = 1

    @model_validator(mode="after")
    def recovery_required(self) -> Self:
        if self.destructive_migration and self.recovery_max_age_hours is None:
            raise ValueError("destructive migrations require a fresh release-specific recovery confirmation")
        return self


class Artifact(Contract):
    url: str = Field(
        pattern=r"^https://raw\.githubusercontent\.com/Promptless/pig-deploy/[a-f0-9]{40}/[a-zA-Z0-9/_.-]+$"
    )
    sha256: Digest


class ChartArtifact(Contract):
    repository: str = Field(pattern=r"^oci://ghcr\.io/promptless/charts/(pig-supervisor|pig-trace-analyzer)$")
    version: str
    sha256: Digest
    oci_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ReleaseArtifacts(Contract):
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    terraform_archive_url: str = Field(
        alias="terraformArchiveURL",
        pattern=r"^https://github\.com/Promptless/pig-deploy/releases/download/v[0-9.]+/pig-deploy-[0-9.]+\.tar\.gz$",
    )
    terraform_archive_sha256: Digest
    supervisor_chart: ChartArtifact
    worker_chart: ChartArtifact


class Release(Contract):
    version: str
    analyzer_image: Image
    supervisor_image: Image
    requirements: Requirements
    # Application rollback is allowed only to listed immutable manifests with the same schema.
    rollback_to: list[Digest] = Field(default_factory=list)
    crd: Artifact | None = None
    artifacts: ReleaseArtifacts | None = None

    @field_validator("version")
    @classmethod
    def stable(cls, value: str) -> str:
        stable_version(value)
        return value


def stable_version(value: str) -> Version:
    """Stable includes every major; prereleases and local build variants are excluded."""
    version = Version(value)
    if (
        version.is_prerelease
        or version.is_devrelease
        or version.local
        or str(version) != value
        or len(version.release) != 3
    ):
        raise ValueError("expected a canonical stable version, e.g. 1.2.3")
    return version
