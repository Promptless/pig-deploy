"""Render generated workloads without creating cloud resources or customer Secrets."""

from __future__ import annotations

from .catalog import canonical_digest
from .models import DeploymentSpec, Release, SecretRef


def env_value(name: str, value: object) -> dict:
    return {"name": name, "value": str(value)}


def env_secret(name: str, ref: SecretRef) -> dict:
    return {"name": name, "valueFrom": {"secretKeyRef": ref.model_dump(by_alias=True)}}


def secret_refs(spec: DeploymentSpec) -> list[SecretRef]:
    return [
        ref
        for ref in (
            spec.hosted.install_token_secret_ref,
            spec.storage.postgres.dsn_secret_ref,
            spec.analysis.model.api_key_secret_ref,
            spec.analysis.repository.token_secret_ref,
        )
        if ref
    ]


def pod_template(spec: DeploymentSpec, release: Release, config_hash: str, deployment_name: str) -> dict:
    """Analyzer and Jobs use the same customer identity, CA, environment, and limits."""
    model, repository = spec.analysis.model, spec.analysis.repository
    values = {
        "RUNTIME_BASE_URL": spec.hosted.runtime_url,
        "DEPLOYMENT_NAME": deployment_name,
        "DEPLOYMENT_INSTANCE_ID": spec.hosted.deployment_id,
        "CONFIG_HASH": config_hash,
        "WORKER_VERSION": release.version,
        "CHART_VERSION": release.version,
        "ANALYSIS_ACTIVATION_AT": spec.analysis.activation_at,
        "ANALYSIS_QUIET_WINDOW_HOURS": spec.analysis.quiet_window_hours,
        "ANALYSIS_REPOSITORY_URL": repository.url,
        "ANALYSIS_REPOSITORY_ID": repository.id,
        "ANALYSIS_REPOSITORY_FULL_NAME": repository.full_name,
        "ANALYSIS_MIRROR_ROOT": "/tmp/analysis-mirrors",
        "ANALYSIS_MODEL_PROVIDER": model.provider,
        "ANALYSIS_MODEL_AUTHENTICATION": model.authentication,
        "ANALYSIS_MODEL_BASE_URL": model.base_url,
        "ANALYSIS_MODEL_NAME": model.name,
    }
    storage = spec.storage
    extra = []
    if storage.s3:
        values.update(
            STORAGE_BACKEND="postgres_s3",
            TRACE_OBJECT_S3_BUCKET=storage.s3.bucket,
            TRACE_OBJECT_S3_PREFIX=storage.s3.prefix,
        )
        extra.append(env_value("AWS_REGION", storage.s3.region))
    elif storage.azure_blob:
        values.update(
            STORAGE_BACKEND="postgres_azure_blob",
            TRACE_OBJECT_AZURE_ACCOUNT_URL=storage.azure_blob.account_url,
            TRACE_OBJECT_AZURE_CONTAINER=storage.azure_blob.container,
            TRACE_OBJECT_PREFIX=storage.azure_blob.prefix,
        )
    elif storage.gcs:
        values.update(
            STORAGE_BACKEND="postgres_gcs",
            TRACE_OBJECT_GCS_BUCKET=storage.gcs.bucket,
            TRACE_OBJECT_PREFIX=storage.gcs.prefix,
        )
    env = [env_value("INSTRUCTION_HUB_" + name, value) for name, value in values.items()] + extra
    env += [
        env_secret("INSTRUCTION_HUB_INSTALL_TOKEN", spec.hosted.install_token_secret_ref),
        env_secret("INSTRUCTION_HUB_CUSTOMER_POSTGRES_DSN", storage.postgres.dsn_secret_ref),
    ]
    for name, ref in (
        ("ANALYSIS_MODEL_API_KEY", model.api_key_secret_ref),
        ("ANALYSIS_REPOSITORY_TOKEN", repository.token_secret_ref),
    ):
        if ref:
            env.append(env_secret("INSTRUCTION_HUB_" + name, ref))
    mounts = [{"name": "tmp", "mountPath": "/tmp"}]
    volumes = [{"name": "tmp", "emptyDir": {}}]
    if storage.postgres.ca_config_map_ref:
        ref = storage.postgres.ca_config_map_ref
        env.append(env_value("PGSSLROOTCERT", "/etc/pig/postgres-ca/ca.pem"))
        mounts.append({"name": "postgres-ca", "mountPath": "/etc/pig/postgres-ca", "readOnly": True})
        volumes.append(
            {"name": "postgres-ca", "configMap": {"name": ref.name, "items": [{"key": ref.key, "path": "ca.pem"}]}}
        )
    labels = dict(spec.pod_labels)
    labels["app.kubernetes.io/name"] = deployment_name + "-analyzer"
    labels["app.kubernetes.io/component"] = "analyzer"
    return {
        "metadata": {"labels": labels, "annotations": {"governance.promptless.ai/config-hash": config_hash}},
        "spec": {
            "serviceAccountName": spec.service_account_name,
            "nodeSelector": dict(spec.node_selector),
            "automountServiceAccountToken": True,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "terminationGracePeriodSeconds": 60,
            "volumes": volumes,
            "containers": [
                {
                    "name": "analyzer",
                    "image": release.analyzer_image,
                    "args": ["serve"],
                    "env": env,
                    "securityContext": {
                        "readOnlyRootFilesystem": True,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"memory": "2Gi"}},
                    "ports": [{"name": "http", "containerPort": 8080}],
                    "volumeMounts": mounts,
                    "readinessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 10},
                    "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "initialDelaySeconds": 30},
                }
            ],
        },
    }


def metadata(deployment: dict, name: str) -> dict:
    parent = deployment["metadata"]
    return {
        "name": name,
        "namespace": parent["namespace"],
        "labels": {"governance.promptless.ai/deployment": parent["name"]},
        "ownerReferences": [
            {
                "apiVersion": deployment["apiVersion"],
                "kind": "PIGDeployment",
                "name": parent["name"],
                "uid": parent["uid"],
                "controller": True,
            }
        ],
    }


def analyzer_resources(deployment: dict, spec: DeploymentSpec, release: Release, config_hash: str) -> list[dict]:
    name = deployment["metadata"]["name"] + "-analyzer"
    labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/component": "analyzer"}
    template = pod_template(spec, release, config_hash, deployment["metadata"]["name"])
    tls: dict[str, object] = {"hosts": [spec.endpoint.hostname]}
    if spec.endpoint.tls_secret_name is not None:
        tls["secretName"] = spec.endpoint.tls_secret_name
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": metadata(deployment, name),
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": labels},
                "template": template,
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": metadata(deployment, name),
            "spec": {"selector": labels, "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]},
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {**metadata(deployment, name), "annotations": spec.endpoint.ingress_annotations},
            "spec": {
                "ingressClassName": spec.endpoint.ingress_class_name,
                "tls": [tls],
                "rules": [
                    {
                        "host": spec.endpoint.hostname,
                        "http": {
                            "paths": [
                                {
                                    "path": path,
                                    "pathType": "Exact",
                                    "backend": {"service": {"name": name, "port": {"name": "http"}}},
                                }
                                for path in (
                                    "/healthz",
                                    "/v0/host-enrollment/policy",
                                    "/v0/host-enrollment/check-ins",
                                    "/v0/traces/batches",
                                )
                            ]
                        },
                    }
                ],
            },
        },
    ]


def job_resource(
    deployment: dict,
    spec: DeploymentSpec,
    release: Release,
    digest: str,
    config_hash: str,
    phase: str,
    attempt: int,
    *,
    transition: str = "",
) -> dict:
    # A rollback/reinstall must run new live checks even if an earlier release's
    # completed Jobs still exist. The transition ID is persisted before Job creation.
    token = canonical_digest([deployment["metadata"]["uid"], digest, config_hash, phase, attempt, transition])[:16]
    name = "pig-" + phase + "-" + token
    template = pod_template(spec, release, config_hash, deployment["metadata"]["name"])
    template["metadata"]["labels"]["governance.promptless.ai/job"] = name
    template["metadata"]["labels"]["app.kubernetes.io/component"] = "maintenance"
    template["spec"]["restartPolicy"] = "Never"
    container = template["spec"]["containers"][0]
    container["args"] = [{"migration": "supervised-migrate"}.get(phase, phase)]
    container["env"] += [
        env_value("PIG_RELEASE_DIGEST", digest),
        env_value("PIG_REQUIREMENTS", release.requirements.model_dump_json(by_alias=True)),
    ]
    for key in ("ports", "readinessProbe", "livenessProbe"):
        container.pop(key)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": metadata(deployment, name),
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,
            "ttlSecondsAfterFinished": 86400,
            "template": template,
        },
    }
