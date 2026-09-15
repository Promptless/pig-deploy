"""Lifecycle and ownership tests exercise restart boundaries with a persisted fake API."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import monotonic

import httpx
import pytest
from pig_supervisor.catalog import CatalogError, SelectedRelease, canonical_digest, select_release
from pig_supervisor.controller import Blocked, Controller, capacity_check, recovery_check
from pig_supervisor.crd import additive_crd
from pig_supervisor.kube import Kube, KubeError
from pig_supervisor.models import DeploymentSpec, Release
from pig_supervisor.workloads import analyzer_resources, job_resource
from pydantic import ValidationError

NOW = datetime(2026, 9, 15, tzinfo=UTC)
ROOT = "https://raw.githubusercontent.com/Promptless/pig-deploy/" + "e" * 40
CATALOG = "https://raw.githubusercontent.com/Promptless/pig-deploy/main/catalog/stable.json"


def manifest(version="1.0.0", **requirements):
    return {
        "version": version,
        "analyzerImage": "ghcr.io/promptless/instruction-hub-worker@sha256:" + "a" * 64,
        "supervisorImage": "ghcr.io/promptless/pig-supervisor@sha256:" + "b" * 64,
        "requirements": {
            "storageBackends": ["s3", "azureBlob", "gcs"],
            "schemaFrom": [0, 1],
            "schemaTo": 1,
            **requirements,
        },
    }


def spec_document():
    return {
        "serviceAccountName": "pig-analyzer",
        "hosted": {
            "runtimeURL": "https://api.promptless.ai",
            "deploymentID": "customer-installation",
            "installTokenSecretRef": {"name": "pig-credentials", "key": "install-token"},
        },
        "endpoint": {"hostname": "pig.example.com", "ingressClassName": "nginx", "tlsSecretName": "pig-tls"},
        "storage": {
            "postgres": {
                "dsnSecretRef": {"name": "pig-credentials", "key": "postgres-dsn"},
                "caConfigMapRef": {"name": "postgres-ca", "key": "ca.pem"},
            },
            "s3": {"bucket": "example-pig", "prefix": "trace-objects", "region": "us-east-2"},
        },
        "analysis": {
            "repository": {
                "url": "https://github.com/example/instructions.git",
                "id": 42,
                "fullName": "example/instructions",
                "tokenSecretRef": {"name": "pig-credentials", "key": "repository-token"},
            },
            "model": {
                "provider": "openai",
                "authentication": "api_key",
                "baseURL": "https://api.openai.com/v1",
                "name": "gpt-5",
                "apiKeySecretRef": {"name": "pig-credentials", "key": "model-api-key"},
            },
        },
    }


def deployment():
    return {
        "apiVersion": "governance.promptless.ai/v1alpha1",
        "kind": "PIGDeployment",
        "metadata": {
            "name": "acme",
            "namespace": "pig",
            "uid": "installation-uid",
            "generation": 1,
            "resourceVersion": "1",
        },
        "spec": spec_document(),
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


class FakeKube:
    def __init__(self):
        self.documents = {}
        self.applied = []
        self.flux = []
        self.documents[("ServiceAccount", "pig", "pig-analyzer")] = {"metadata": {"resourceVersion": "1"}}
        self.documents[("Secret", "pig", "pig-credentials")] = {
            "metadata": {"resourceVersion": "1"},
            "data": {key: "opaque" for key in ("install-token", "postgres-dsn", "model-api-key", "repository-token")},
        }
        self.documents[("ConfigMap", "pig", "postgres-ca")] = {
            "metadata": {"resourceVersion": "1"},
            "data": {"ca.pem": "public-ca"},
        }
        self.documents[("Deployment", "pig-system", "pig-supervisor")] = {
            "metadata": {"generation": 1},
            "spec": {"template": {"spec": {"containers": [{"name": "supervisor", "image": "bootstrap"}]}}},
            "status": {"observedGeneration": 1, "updatedReplicas": 1, "availableReplicas": 1, "replicas": 1},
        }

    def get(self, kind, namespace, name):
        return self.documents.get((kind, namespace, name))

    def list(self, kind, namespace):
        return self.flux if kind == "HelmRelease" else []

    def apply(self, resource):
        self.applied.append(deepcopy(resource))
        key = resource["kind"], resource["metadata"]["namespace"], resource["metadata"]["name"]
        previous = self.documents.get(key, {})
        result = deepcopy(resource)
        if resource["kind"] == "Deployment":
            result["metadata"]["generation"] = 1
            result["status"] = {"observedGeneration": 1, "updatedReplicas": 1, "availableReplicas": 1, "replicas": 1}
        else:
            result["status"] = previous.get("status", {})
        self.documents[key] = result
        return result

    def patch(self, kind, namespace, name, patch):
        resource = self.get(kind, namespace, name)
        if "replicas" in patch.get("spec", {}):
            resource["status"]["replicas"] = patch["spec"]["replicas"]
        else:
            resource["spec"]["template"]["spec"]["containers"][0]["image"] = patch["spec"]["template"]["spec"][
                "containers"
            ][0]["image"]
        return resource

    def request(self, method, path, **kwargs):
        assert method == "GET" and path == "/version"
        return {"gitVersion": "v1.32.1-eks-example"}

    def status(self, document, status):
        document["status"] = deepcopy(status)

    def finish_jobs(self, succeeded=True):
        for (kind, _, _), resource in self.documents.items():
            if kind == "Job":
                resource["status"] = {"succeeded" if succeeded else "failed": 1}


def reconcile_to_ready(kube, document, client):
    for _ in range(24):
        # A fresh Controller on every step proves phase progress does not live in process memory.
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
        kube.finish_jobs()
        if document.get("status", {}).get("currentVersion"):
            return
    pytest.fail(str(document.get("status")))


def reason(document):
    return next(c["reason"] for c in document["status"]["conditions"] if c["type"] == "Blocked")


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


def test_install_is_restartable_and_ready_is_distinct_from_acceptance():
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
        assert document["status"]["currentVersion"] == "1.0.0"
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 1, NOW)
        conditions = {c["type"]: c["status"] for c in document["status"]["conditions"]}
        assert conditions["Ready"] == "True" and conditions["Acceptance"] == "False"
        kube.finish_jobs()
        controller.reconcile(document, 1, NOW + timedelta(seconds=15))
        assert next(c for c in document["status"]["conditions"] if c["type"] == "Acceptance")["status"] == "True"
        assert {r["kind"] for r in kube.applied} == {"Deployment", "Service", "Ingress", "Job"}


def test_failed_preflight_retries_and_resumes_after_external_fix():
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 1, NOW)
        controller.reconcile(document, 1, NOW)
        kube.finish_jobs(False)
        controller.reconcile(document, 1, NOW)
        assert reason(document) == "DependencyCheckFailed"
        assert not any(r["kind"] == "Deployment" for r in kube.applied)
        controller.reconcile(document, 1, NOW + timedelta(minutes=3))
        controller.reconcile(document, 1, NOW + timedelta(minutes=3))
        assert len([r for r in kube.applied if r["kind"] == "Job"]) == 2
        kube.finish_jobs()
        reconcile_to_ready(kube, document, client)


def test_competing_policy_and_flux_ownership_block_before_any_workload():
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 2, NOW)
        assert reason(document) == "MultipleDeployments" and not kube.applied
        kube.flux = [{"metadata": {"name": "pig-supervisor"}, "spec": {"suspend": False}}]
        controller.reconcile(document, 1, NOW)
        assert reason(document) == "FluxHandoffRequired" and not kube.applied


def test_secret_rotation_revalidates_and_never_overwrites_secret_or_serviceaccount():
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
        previous = document["status"]["configurationHash"]
        kube.get("Secret", "pig", "pig-credentials")["metadata"]["resourceVersion"] = "2"
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
        assert document["status"]["phase"] == "preflight"
        assert document["status"]["transitionConfigurationHash"] != previous
        assert not {"Secret", "ServiceAccount", "ConfigMap"} & {r["kind"] for r in kube.applied}


def test_paused_policy_uses_installed_manifest_without_network():
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    document["spec"]["release"] = {"paused": True}
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected catalog fetch"))
    ) as offline:
        Controller(kube, offline, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
    assert not document["status"].get("targetDigest")


def test_recovery_is_exact_fresh_and_rechecked_after_pause():
    spec = DeploymentSpec.model_validate(spec_document())
    selected = SelectedRelease(Release.model_validate(manifest(recoveryMaxAgeHours=24)), "c" * 64)
    data = {
        "releaseDigest": "sha256:" + selected.digest,
        "deploymentID": spec.hosted.deployment_id,
        "postgresRecoveryPoint": "snapshot-example",
        "objectRecoveryPoint": "version-window-example",
        "confirmedAt": NOW.isoformat(),
    }
    recovery_check(spec, selected, {"data": data}, NOW)
    for overrides in (
        {"releaseDigest": "d" * 64},
        {"deploymentID": "another-installation"},
        {"confirmedAt": (NOW - timedelta(hours=25)).isoformat()},
        {"confirmedAt": (NOW + timedelta(hours=1)).isoformat()},
        {"postgresRecoveryPoint": ""},
    ):
        with pytest.raises(Blocked, match="Refresh"):
            recovery_check(spec, selected, {"data": {**data, **overrides}}, NOW)


def test_new_capability_cannot_grant_rbac():
    kube, document = FakeKube(), deployment()
    with client_for(manifest(capabilities=["grant-more-rbac"])) as client:
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
    assert reason(document) == "RBACUpgradeRequired" and not kube.applied


@pytest.mark.parametrize(
    "backend,block,label",
    [
        ("s3", {"region": "us-east-2", "bucket": "example-pig", "prefix": "trace-objects"}, {}),
        (
            "azureBlob",
            {
                "accountURL": "https://examplepig.blob.core.windows.net",
                "container": "traces",
                "prefix": "trace-objects",
            },
            {"azure.workload.identity/use": "true"},
        ),
        ("gcs", {"bucket": "example-pig", "prefix": "trace-objects"}, {}),
    ],
)
def test_native_identity_ca_and_jobs_cannot_receive_service_traffic(backend, block, label):
    document = deployment()
    document["spec"]["storage"].pop("s3")
    document["spec"]["storage"][backend] = block
    document["spec"]["podLabels"] = label
    spec = DeploymentSpec.model_validate(document["spec"])
    release = Release.model_validate(manifest())
    resources = analyzer_resources(document, spec, release, "configuration")
    assert [r["metadata"]["name"] for r in resources] == ["acme-analyzer"] * 3
    template = resources[0]["spec"]["template"]
    assert template["spec"]["serviceAccountName"] == "pig-analyzer"
    assert {e["name"]: e for e in template["spec"]["containers"][0]["env"]}["PGSSLROOTCERT"][
        "value"
    ] == "/etc/pig/postgres-ca/ca.pem"
    assert all(template["metadata"]["labels"][k] == v for k, v in label.items())
    job = job_resource(document, spec, release, "c" * 64, "configuration", "migration", 0)
    selector = resources[1]["spec"]["selector"]
    assert any(job["spec"]["template"]["metadata"]["labels"].get(k) != v for k, v in selector.items())
    assert job["spec"]["template"]["spec"]["containers"][0]["args"] == ["supervised-migrate"]


def test_kubernetes_rotates_tokens_and_fences_expired_lease(tmp_path):
    token = tmp_path / "token"
    token.write_text("first")
    seen = []

    def respond(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={})

    kube = Kube(httpx.Client(base_url="https://kubernetes.example", transport=httpx.MockTransport(respond)), token)
    kube.request("GET", "/version")
    token.write_text("second")
    kube.request("GET", "/version")
    assert seen == ["Bearer first", "Bearer second"]
    with pytest.raises(KubeError):
        kube.patch("Deployment", "pig", "analyzer", {"spec": {"replicas": 0}})
    kube.lease_deadline = monotonic() + 30
    kube.patch("Deployment", "pig", "analyzer", {"spec": {"replicas": 0}})


def test_kubernetes_refuses_adoption():
    def respond(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"metadata": {"ownerReferences": [{"uid": "other"}]}})

    kube = Kube(httpx.Client(base_url="https://kubernetes.example", transport=httpx.MockTransport(respond)))
    resource = analyzer_resources(
        deployment(), DeploymentSpec.model_validate(spec_document()), Release.model_validate(manifest()), "hash"
    )[0]
    with pytest.raises(KubeError) as error:
        kube.apply(resource)
    assert error.value.status == 409


def test_capacity_acknowledgement_binds_release_requirements_and_expires():
    spec = DeploymentSpec.model_validate(spec_document())
    selected = SelectedRelease(
        Release.model_validate(
            manifest(operatorCapacityRequirements=["Allow two times the table size for migration."])
        ),
        "c" * 64,
    )
    data = {
        "releaseDigest": "sha256:" + selected.digest,
        "deploymentID": spec.hosted.deployment_id,
        "capacityRequirementsDigest": "sha256:"
        + canonical_digest(selected.release.requirements.operator_capacity_requirements),
        "capacityConfirmedAt": NOW.isoformat(),
        "capacityEvidence": "Operator checked managed database metrics after Terraform change.",
    }
    capacity_check(spec, selected, {"data": data}, NOW)
    for overrides in (
        {"releaseDigest": "sha256:" + "d" * 64},
        {"capacityRequirementsDigest": "sha256:" + "d" * 64},
        {"capacityConfirmedAt": (NOW - timedelta(days=2)).isoformat()},
        {"capacityEvidence": ""},
    ):
        with pytest.raises(Blocked):
            capacity_check(spec, selected, {"data": {**data, **overrides}}, NOW)
    with pytest.raises(Blocked):
        capacity_check(spec, selected, None, NOW)
    # A recovery confirmation cannot stand in for the separate capacity acknowledgement.
    with pytest.raises(Blocked):
        capacity_check(
            spec, selected, {"data": {"confirmedAt": NOW.isoformat(), "postgresRecoveryPoint": "snapshot"}}, NOW
        )


def test_confirmation_arrival_resumes_without_a_terraform_inventory():
    kube, document = FakeKube(), deployment()
    document["spec"]["release"] = {"confirmation": {"configMapRef": "release-confirmation"}}
    with client_for(manifest(operatorCapacityRequirements=["Customer must verify disk headroom."])) as client:
        selected = select_release(client, CATALOG)
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 1, NOW)
        assert reason(document) == "CapacityConfirmationRequired" and not kube.applied
        kube.documents[("ConfigMap", "pig", "release-confirmation")] = {
            "data": {
                "releaseDigest": "sha256:" + selected.digest,
                "deploymentID": "customer-installation",
                "capacityRequirementsDigest": "sha256:"
                + canonical_digest(selected.release.requirements.operator_capacity_requirements),
                "capacityConfirmedAt": NOW.isoformat(),
                "capacityEvidence": "Metrics reviewed.",
            }
        }
        reconcile_to_ready(kube, document, client)
        assert document["status"]["currentDigest"] == selected.digest


def test_destructive_release_cannot_omit_recovery_prerequisite():
    with pytest.raises(ValidationError):
        Release.model_validate(manifest(destructiveMigration=True))


def test_shared_crd_only_allows_optional_additions():
    from pathlib import Path

    import yaml

    original = yaml.safe_load(Path("charts/pig-supervisor/crds/pigdeployments.yaml").read_text())["spec"]
    current = deepcopy(original)
    current["names"]["listKind"] = "PIGDeploymentList"
    current["conversion"] = {"strategy": "None"}
    target = deepcopy(original)
    schema = target["versions"][0]["schema"]["openAPIV3Schema"]
    schema["properties"]["spec"]["properties"]["optionalNewField"] = {"type": "string"}
    assert additive_crd(current, target)
    schema["properties"]["spec"]["required"].append("optionalNewField")
    assert not additive_crd(current, target)
    assert not additive_crd(target, original)


def test_forward_repair_can_replace_a_failed_target_at_safe_checkpoint():
    kube, document = FakeKube(), deployment()
    with client_for(manifest("1.0.0")) as client:
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 1, NOW)
        controller.reconcile(document, 1, NOW)
        kube.finish_jobs(False)
        controller.reconcile(document, 1, NOW)
        assert reason(document) == "DependencyCheckFailed"
    with client_for(manifest("1.0.0"), manifest("1.0.1")) as client:
        reconcile_to_ready(kube, document, client)
    assert document["status"]["currentVersion"] == "1.0.1"


def test_paused_secret_rotation_finishes_offline_without_repeating_migration():
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    previous = document["status"]["configurationHash"]
    migrations_before = [
        r["metadata"]["name"]
        for r in kube.applied
        if r["kind"] == "Job" and r["spec"]["template"]["spec"]["containers"][0]["args"] == ["supervised-migrate"]
    ]
    document["spec"]["release"] = {"paused": True}
    kube.get("Secret", "pig", "pig-credentials")["metadata"]["resourceVersion"] = "2"
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected catalog fetch"))
    ) as offline:
        for _ in range(24):
            Controller(kube, offline, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
            kube.finish_jobs()
    assert document["status"]["configurationHash"] != previous
    assert not document["status"].get("targetDigest")
    assert reason(document) == "ChecksPassed"
    assert migrations_before == [
        r["metadata"]["name"]
        for r in kube.applied
        if r["kind"] == "Job" and r["spec"]["template"]["spec"]["containers"][0]["args"] == ["supervised-migrate"]
    ]


@pytest.mark.parametrize("authentication", ["api_key", "aws_sigv4"])
def test_bedrock_supports_both_existing_authentication_contracts(authentication):
    document = spec_document()
    model = document["analysis"]["model"]
    model.update(
        provider="aws_bedrock", authentication=authentication, baseURL="https://bedrock-mantle.us-east-1.api.aws/v1"
    )
    if authentication == "aws_sigv4":
        model.pop("apiKeySecretRef")
    assert DeploymentSpec.model_validate(document).analysis.model.authentication == authentication


def test_confirmation_expiry_after_pause_blocks_creation_of_destructive_migration():
    kube, document = FakeKube(), deployment()
    document["spec"]["release"] = {"confirmation": {"configMapRef": "release-confirmation"}}
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    target = manifest("2.0.0", destructiveMigration=True, recoveryMaxAgeHours=24)
    with client_for(manifest(), target) as client:
        selected = select_release(client, CATALOG)
        confirmation = {
            "data": {
                "releaseDigest": "sha256:" + selected.digest,
                "deploymentID": "customer-installation",
                "postgresRecoveryPoint": "snapshot-example",
                "objectRecoveryPoint": "version-window-example",
                "confirmedAt": NOW.isoformat(),
            }
        }
        kube.documents[("ConfigMap", "pig", "release-confirmation")] = confirmation
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        for _ in range(12):
            controller.reconcile(document, 1, NOW)
            kube.finish_jobs()
            if document["status"]["phase"] == "migration":
                break
        assert document["status"]["phase"] == "migration"
        before = len(kube.applied)
        document["spec"]["release"]["paused"] = True
        later = NOW + timedelta(hours=25)
        controller.reconcile(document, 1, later)
        assert reason(document) == "Paused" and len(kube.applied) == before
        document["spec"]["release"]["paused"] = False
        controller.reconcile(document, 1, later)
        assert reason(document) == "RecoveryConfirmationInvalid" and len(kube.applied) == before
        confirmation["data"]["confirmedAt"] = later.isoformat()
        controller.reconcile(document, 1, later)
        assert len(kube.applied) == before + 1
        assert kube.applied[-1]["spec"]["template"]["spec"]["containers"][0]["args"] == ["supervised-migrate"]


def test_failed_target_cannot_restore_old_analyzer_after_incompatible_migration():
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    with client_for(manifest(), manifest("2.0.0", schemaFrom=[1], schemaTo=2)) as client:
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        for _ in range(18):
            controller.reconcile(document, 1, NOW)
            kube.finish_jobs()
            if document["status"]["phase"] == "verify":
                break
        assert document["status"]["phase"] == "verify"
        before = len(kube.applied)
        document["spec"]["release"] = {"pinnedVersion": "1.0.0"}
        controller.reconcile(document, 1, NOW)
        assert reason(document) == "RollbackUnsupported"
        assert document["status"]["targetVersion"] == "2.0.0"
        assert len(kube.applied) == before


def test_compatible_rollback_runs_fresh_preflight_instead_of_reusing_old_success():
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        original = select_release(client, CATALOG)
        reconcile_to_ready(kube, document, client)
    target = {**manifest("2.0.0"), "rollbackTo": [original.digest]}
    with client_for(manifest(), target) as client:
        for _ in range(24):
            Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
            kube.finish_jobs()
            if document["status"].get("currentVersion") == "2.0.0":
                break
        assert document["status"]["currentVersion"] == "2.0.0"
        old_jobs = {name for kind, _, name in kube.documents if kind == "Job"}
        document["spec"]["release"] = {"pinnedVersion": "1.0.0"}
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 1, NOW)
        assert document["status"]["phase"] == "preflight"
        controller.reconcile(document, 1, NOW)
        created = kube.applied[-1]
        assert created["kind"] == "Job" and created["metadata"]["name"] not in old_jobs
        assert created["spec"]["template"]["spec"]["containers"][0]["args"] == ["preflight"]
        assert document["status"]["phase"] == "preflight"
        controller.reconcile(document, 1, NOW)
        assert document["status"]["phase"] == "preflight"
