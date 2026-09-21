"""Lifecycle and ownership tests exercise restart boundaries with a persisted fake API."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import monotonic

import httpx
import pytest
from pig_supervisor.catalog import CatalogError, SelectedRelease, canonical_digest, select_release
from pig_supervisor.controller import Blocked, Controller, capacity_check, recovery_check
from pig_supervisor.crd import additive_crd, merge_crd
from pig_supervisor.kube import Kube, KubeError
from pig_supervisor.models import DeploymentSpec, Release
from pig_supervisor.workloads import analyzer_resources, job_resource
from pydantic import ValidationError

NOW = datetime(2026, 9, 15, tzinfo=UTC)
ROOT = "https://raw.githubusercontent.com/Promptless/pig-deploy/" + "e" * 40
CATALOG = "https://raw.githubusercontent.com/Promptless/pig-deploy/main/catalog/stable.json"
CANDIDATE_REQUIREMENTS = json.loads(
    (Path(__file__).resolve().parents[1] / "releases/requirements/0.3.0.json").read_text()
)


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


def spec_document():
    return {
        "serviceAccountName": "pig-analyzer",
        "hosted": {
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


@pytest.mark.parametrize("runtime_url", [None, "https://staging.example.com/"])
def test_installation_credential_supplies_identity_for_every_workload(runtime_url):
    document = deployment()
    if runtime_url is not None:
        document["spec"]["hosted"]["runtimeURL"] = runtime_url
    spec = DeploymentSpec.model_validate(document["spec"])
    release = Release.model_validate(manifest())
    resources = [analyzer_resources(document, spec, release, "hash")[0]]
    resources.extend(
        job_resource(document, spec, release, "a" * 64, "hash", phase, 0)
        for phase in ("preflight", "migration", "verify", "acceptance")
    )
    for resource in resources:
        env = {entry["name"]: entry for entry in resource["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["INSTRUCTION_HUB_RUNTIME_BASE_URL"]["value"] == (
            runtime_url.rstrip("/") if runtime_url else "https://api.gopromptless.ai"
        )
        assert "INSTRUCTION_HUB_DEPLOYMENT_INSTANCE_ID" not in env
        assert "INSTRUCTION_HUB_DEPLOYMENT_NAME" not in env
        assert not any(name.startswith("INSTRUCTION_HUB_ANALYSIS_REPOSITORY_") for name in env)
        assert env["INSTRUCTION_HUB_ANALYSIS_MODEL_NAME"]["value"] == "gpt-5"
        assert env["INSTRUCTION_HUB_INSTALL_TOKEN"]["valueFrom"]["secretKeyRef"] == {
            "name": "pig-credentials",
            "key": "install-token",
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
        self.documents: dict[tuple[str, str, str], dict] = {}
        self.applied = []
        self.flux = []
        self.pods = []
        self.rollout_failure = False
        self.documents[("ServiceAccount", "pig", "pig-analyzer")] = {"metadata": {"resourceVersion": "1"}}
        self.documents[("Secret", "pig", "pig-credentials")] = {
            "metadata": {"resourceVersion": "1"},
            "data": {key: "opaque" for key in ("install-token", "postgres-dsn", "model-api-key")},
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

    def list(self, kind, namespace, selector=""):
        if kind == "HelmRelease":
            return self.flux
        labels = {key: value for key, _, value in (part.partition("=") for part in selector.split(",") if part)}
        return [pod for pod in self.pods if all(pod["metadata"]["labels"].get(k) == v for k, v in labels.items())]

    def apply(self, resource):
        self.applied.append(deepcopy(resource))
        key = resource["kind"], resource["metadata"]["namespace"], resource["metadata"]["name"]
        previous = self.documents.get(key, {})
        result = deepcopy(resource)
        if resource["kind"] == "Deployment":
            generation = previous.get("metadata", {}).get("generation", 0)
            generation += previous.get("spec") != resource["spec"]
            result["metadata"]["generation"] = generation
            replicas = resource["spec"]["replicas"]
            result["status"] = {
                "observedGeneration": generation,
                "updatedReplicas": replicas,
                "availableReplicas": replicas,
                "replicas": replicas,
            }
            if self.rollout_failure and replicas:
                result["status"].update(
                    availableReplicas=0,
                    conditions=[{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}],
                )
        else:
            result["status"] = previous.get("status", {})
        self.documents[key] = result
        return result

    def patch(self, kind, namespace, name, patch):
        resource = self.get(kind, namespace, name)
        if "replicas" in patch.get("spec", {}):
            resource["spec"]["replicas"] = patch["spec"]["replicas"]
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
                resource["status"] = {
                    "succeeded" if succeeded else "failed": 1,
                    "conditions": [{"type": "Complete" if succeeded else "Failed", "status": "True"}],
                }


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


def upgrade_to_phase(kube, document, client, phase):
    for _ in range(24):
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
        if document["status"]["phase"] == phase and document["status"].get("targetVersion") == "2.0.0":
            return
        kube.finish_jobs()
    pytest.fail(str(document["status"]))


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


@pytest.mark.parametrize("tls_secret_name", ["pig-tls", None])
def test_ingress_supports_secret_and_controller_managed_certificates(tls_secret_name):
    document = deployment()
    endpoint = document["spec"]["endpoint"]
    if tls_secret_name is None:
        endpoint.pop("tlsSecretName")
        endpoint["ingressClassName"] = "alb"
        endpoint["ingressAnnotations"] = {
            "alb.ingress.kubernetes.io/certificate-arn": "arn:aws:acm:us-east-2:123456789012:certificate/example",
            "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
        }
    kube = FakeKube()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    ingress = kube.get("Ingress", "pig", "acme-analyzer")
    tls = ingress["spec"]["tls"][0]
    assert tls["hosts"] == [endpoint["hostname"]]
    if tls_secret_name is None:
        assert "secretName" not in tls
        assert ingress["metadata"]["annotations"] == endpoint["ingressAnnotations"]
        assert ingress["spec"]["ingressClassName"] == "alb"
    else:
        assert tls["secretName"] == tls_secret_name


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


@pytest.mark.parametrize(
    "name,spec,system_namespace,supervisor_name,matching",
    [
        ("bootstrap", {"targetNamespace": "pig-system"}, "pig-system", "pig-system-bootstrap", True),
        ("bootstrap", {}, "pig-system", "bootstrap", True),
        ("bootstrap", {"releaseName": "custom", "targetNamespace": "pig-system"}, "pig-system", "custom", True),
        (
            "with-a-nice-object-name",
            {"targetNamespace": "a-very-lengthy-target-namespace"},
            "a-very-lengthy-target-namespace",
            "a-very-lengthy-target-namespace-with-a-n-97af5d7f41f3",
            True,
        ),
        ("bootstrap", {"releaseName": "custom", "targetNamespace": "other"}, "pig-system", "custom", False),
        ("unrelated", {}, "pig-system", "pig-supervisor", False),
    ],
)
@pytest.mark.parametrize("suspended", [False, True])
def test_flux_handoff_matches_installed_release_identity(
    name, spec, system_namespace, supervisor_name, matching, suspended
):
    kube, document = FakeKube(), deployment()
    kube.flux = [{"metadata": {"name": name, "namespace": system_namespace}, "spec": spec | {"suspend": suspended}}]
    with client_for(manifest()) as client:
        controller = Controller(kube, client, CATALOG, "pig", system_namespace, supervisor_name)
        for _ in range(2):
            controller.reconcile(document, 1, NOW)
        if matching and not suspended:
            assert reason(document) == "FluxHandoffRequired" and not kube.applied
        else:
            assert reason(document) == "ChecksPassed"
            assert [resource["kind"] for resource in kube.applied] == ["Job"]


@pytest.mark.parametrize("change", ["Secret", "ServiceAccount", "ConfigMap", "release"])
def test_acceptance_is_invalidated_through_transition_and_renewed_only_with_matching_evidence(change):
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 1, NOW)
        kube.finish_jobs()
        controller.reconcile(document, 1, NOW)
    previous = deepcopy(document["status"])
    assert next(c for c in previous["conditions"] if c["type"] == "Acceptance")["status"] == "True"
    if change != "release":
        name = {"Secret": "pig-credentials", "ServiceAccount": "pig-analyzer", "ConfigMap": "postgres-ca"}[change]
        kube.get(change, "pig", name)["metadata"]["resourceVersion"] = "2"
    releases = [manifest(), manifest("2.0.0")] if change == "release" else [manifest()]
    with client_for(*releases) as client:
        for _ in range(24):
            controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
            controller.reconcile(document, 1, NOW)
            status = document["status"]
            assert next(c for c in status["conditions"] if c["type"] == "Acceptance")["status"] == "False"
            for key in ("acceptedConfigurationHash", "acceptedReleaseDigest", "acceptedJob", "lastAcceptanceAt"):
                assert status[key] == previous[key]
            if status["phase"] == "complete" and not status.get("targetDigest"):
                break
            kube.finish_jobs()
        else:
            pytest.fail("transition never completed")
        assert next(c for c in status["conditions"] if c["type"] == "Ready")["status"] == "True"
        assert (status["configurationHash"], status["currentDigest"]) != (
            previous["configurationHash"],
            previous["currentDigest"],
        )
        # The next steady reconcile starts a new acceptance Job; it is not evidence yet.
        controller.reconcile(document, 1, NOW)
        assert next(c for c in document["status"]["conditions"] if c["type"] == "Acceptance")["status"] == "False"
        kube.finish_jobs()
        controller.reconcile(document, 1, NOW)
        status = document["status"]
        assert next(c for c in status["conditions"] if c["type"] == "Acceptance")["status"] == "True"
        assert status["acceptedConfigurationHash"] == status["configurationHash"]
        assert status["acceptedReleaseDigest"] == status["currentDigest"]
        assert status["acceptedJob"] != previous["acceptedJob"]
    assert document["metadata"]["generation"] == 1


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
    deployment_uid = deployment()["metadata"]["uid"]
    selected = SelectedRelease(Release.model_validate(manifest(recoveryMaxAgeHours=24)), "c" * 64)
    data = {
        "releaseDigest": "sha256:" + selected.digest,
        "deploymentUID": deployment_uid,
        "postgresRecoveryPoint": "snapshot-example",
        "objectRecoveryPoint": "version-window-example",
        "confirmedAt": NOW.isoformat(),
    }
    recovery_check(deployment_uid, selected, {"data": data}, NOW)
    for overrides in (
        {"releaseDigest": "d" * 64},
        {"deploymentUID": "another-installation"},
        {"confirmedAt": (NOW - timedelta(hours=25)).isoformat()},
        {"confirmedAt": (NOW + timedelta(hours=1)).isoformat()},
        {"postgresRecoveryPoint": ""},
    ):
        with pytest.raises(Blocked, match="Refresh"):
            recovery_check(deployment_uid, selected, {"data": {**data, **overrides}}, NOW)


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
    deployment_uid = deployment()["metadata"]["uid"]
    selected = SelectedRelease(
        Release.model_validate(
            manifest(operatorCapacityRequirements=["Allow two times the table size for migration."])
        ),
        "c" * 64,
    )
    data = {
        "releaseDigest": "sha256:" + selected.digest,
        "deploymentUID": deployment_uid,
        "capacityRequirementsDigest": "sha256:"
        + canonical_digest(selected.release.requirements.operator_capacity_requirements),
        "capacityConfirmedAt": NOW.isoformat(),
        "capacityEvidence": "Operator checked managed database metrics after Terraform change.",
    }
    capacity_check(deployment_uid, selected, {"data": data}, NOW)
    for overrides in (
        {"releaseDigest": "sha256:" + "d" * 64},
        {"deploymentUID": "replacement-deployment-uid"},
        {"capacityRequirementsDigest": "sha256:" + "d" * 64},
        {"capacityConfirmedAt": (NOW - timedelta(days=2)).isoformat()},
        {"capacityEvidence": ""},
    ):
        with pytest.raises(Blocked):
            capacity_check(deployment_uid, selected, {"data": {**data, **overrides}}, NOW)
    with pytest.raises(Blocked):
        capacity_check(deployment_uid, selected, None, NOW)
    # A recovery confirmation cannot stand in for the separate capacity acknowledgement.
    with pytest.raises(Blocked):
        capacity_check(
            deployment_uid,
            selected,
            {"data": {"confirmedAt": NOW.isoformat(), "postgresRecoveryPoint": "snapshot"}},
            NOW,
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
                "deploymentUID": document["metadata"]["uid"],
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


def test_shared_crd_preserves_superset_and_unions_optional_fields():
    from pathlib import Path

    import yaml

    original = yaml.safe_load(Path("charts/pig-supervisor/crds/pigdeployments.yaml").read_text())["spec"]
    installed = deepcopy(original)
    installed["conversion"] = {"strategy": "None"}
    installed["names"]["listKind"] = "PIGDeploymentList"
    installed["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["newerField"] = {"type": "string"}
    assert merge_crd(installed, original) == installed
    target = deepcopy(original)
    target["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["otherField"] = {"type": "boolean"}
    merged = merge_crd(installed, target)
    assert additive_crd(installed, merged) and additive_crd(target, merged)
    assert merge_crd(merged, target) == merged
    target["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["newerField"] = {"type": "integer"}
    assert merge_crd(installed, target) is None


@pytest.mark.parametrize("superset", [False, True])
def test_self_update_accepts_shared_crd_without_removing_new_fields(superset):
    from pathlib import Path

    import yaml

    crd = yaml.safe_load(Path("charts/pig-supervisor/crds/pigdeployments.yaml").read_text())
    payload = yaml.safe_dump(crd).encode()
    installed = deepcopy(crd)
    installed["metadata"]["resourceVersion"] = "7"
    newer = installed if superset else crd
    newer["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["futureField"] = {"type": "string"}
    if not superset:
        payload = yaml.safe_dump(crd).encode()
    release = manifest()
    release["crd"] = {"url": ROOT + "/crd.yaml", "sha256": sha256(payload).hexdigest()}
    patches = []
    kube = FakeKube()

    def request(method, path, **kwargs):
        if method == "GET":
            return installed
        patches.append(kwargs["json"])

    kube.request = request
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))) as client:
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")._self_update(
            SelectedRelease(Release.model_validate(release), "a" * 64)
        )
    if superset:
        assert patches == []
    else:
        assert patches == [{"metadata": {"resourceVersion": "7"}, "spec": crd["spec"]}]
    assert (
        kube.get("Deployment", "pig-system", "pig-supervisor")["spec"]["template"]["spec"]["containers"][0]["image"]
        == release["supervisorImage"]
    )


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


@pytest.mark.parametrize("requirements", [{}, CANDIDATE_REQUIREMENTS], ids=["schema-1", "schema-2-candidate"])
def test_paused_secret_rotation_finishes_offline_without_repeating_migration(requirements):
    kube, document = FakeKube(), deployment()
    with client_for(manifest(**requirements)) as client:
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


@pytest.mark.parametrize("phase", ["preflight", "quiesce"])
@pytest.mark.parametrize(
    "kind,name", [("Secret", "pig-credentials"), ("ConfigMap", "postgres-ca"), ("ServiceAccount", "pig-analyzer")]
)
def test_pause_pending_upgrade_rotates_installed_release_offline(phase, kind, name):
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    target = manifest("2.0.0")
    target["analyzerImage"] = target["analyzerImage"].replace("a" * 64, "c" * 64)
    with client_for(manifest(), target) as client:
        upgrade_to_phase(kube, document, client, phase)
    previous = document["status"]["configurationHash"]
    before = len(kube.applied)
    document["spec"]["release"] = {"paused": True}
    kube.get(kind, "pig", name)["metadata"]["resourceVersion"] = "2"
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected catalog fetch"))) as client:
        for _ in range(24):
            Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
            kube.finish_jobs()
    assert document["status"]["currentVersion"] == "1.0.0"
    assert document["status"]["configurationHash"] != previous
    assert not document["status"].get("targetDigest")
    applied = kube.applied[before:]
    assert not any(
        r["kind"] == "Job" and r["spec"]["template"]["spec"]["containers"][0]["args"] == ["supervised-migrate"]
        for r in applied
    )
    for resource in applied:
        if resource["kind"] in {"Deployment", "Job"}:
            assert resource["spec"]["template"]["spec"]["containers"][0]["image"] == manifest()["analyzerImage"]
    document["spec"]["release"]["paused"] = False
    with client_for(manifest(), target) as client:
        for _ in range(24):
            Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
            kube.finish_jobs()
    assert document["status"]["currentVersion"] == "2.0.0"


@pytest.mark.parametrize("legacy_status", [False, True])
def test_pause_never_restores_old_release_after_migration_configuration_reset(legacy_status):
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    with client_for(manifest(), manifest("2.0.0", schemaTo=2)) as client:
        upgrade_to_phase(kube, document, client, "verify")
        kube.get("Secret", "pig", "pig-credentials")["metadata"]["resourceVersion"] = "2"
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
    assert document["status"]["phase"] == "preflight"
    assert document["status"]["migrationMayHaveRun"] is True
    if legacy_status:
        document["status"].pop("migrationMayHaveRun")
    document["spec"]["release"] = {"paused": True}
    before = len(kube.applied)
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected catalog fetch"))) as client:
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
    assert reason(document) == "Paused"
    assert len(kube.applied) == before
    assert document["status"]["targetVersion"] == "2.0.0"


@pytest.mark.parametrize(
    "phase,expected",
    [("preflight", "DependencyCheckFailed"), ("migration", "MigrationBlocked"), ("verify", "VerificationBlocked")],
)
def test_deadline_without_pods_retries_phase_job(phase, expected):
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    with client_for(manifest(), manifest("2.0.0")) as client:
        upgrade_to_phase(kube, document, client, phase)
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 1, NOW)
        job = kube.get("Job", "pig", kube.applied[-1]["metadata"]["name"])
        job["status"] = {"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]}
        controller.reconcile(document, 1, NOW)
        assert reason(document) == expected
        assert document["status"]["retryAt"] == (NOW + timedelta(minutes=2)).isoformat()
        controller.reconcile(document, 1, NOW + timedelta(minutes=3))
        controller.reconcile(document, 1, NOW + timedelta(minutes=3))
        assert kube.applied[-1]["metadata"]["name"] != job["metadata"]["name"]
        assert document["status"]["attempt"] == 1


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("legacy_status", [False, True])
def test_repair_waits_for_terminal_migration_condition(terminal, legacy_status):
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    with client_for(manifest(), manifest("2.0.0")) as client:
        upgrade_to_phase(kube, document, client, "migration")
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
    job = kube.get("Job", "pig", kube.applied[-1]["metadata"]["name"])
    job["status"] = {"conditions": [{"type": "Failed" if terminal else "FailureTarget", "status": "True"}]}
    if legacy_status:
        document["status"].pop("migrationMayHaveRun")
    document["spec"]["release"] = {"pinnedVersion": "2.0.1"}
    with client_for(manifest(), manifest("2.0.0"), manifest("2.0.1")) as client:
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
    assert document["status"]["targetVersion"] == ("2.0.1" if terminal else "2.0.0")
    if terminal or not legacy_status:
        assert document["status"]["migrationMayHaveRun"] is True
    if not terminal:
        assert reason(document) == "WaitingForMigration"


def test_acceptance_deadline_without_pods_schedules_new_check():
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 1, NOW)
        job = kube.get("Job", "pig", kube.applied[-1]["metadata"]["name"])
        job["status"] = {"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]}
        controller.reconcile(document, 1, NOW)
        assert document["status"]["acceptanceNextCheck"] == (NOW + timedelta(seconds=120)).isoformat()
        assert "acceptedJob" not in document["status"]
        controller.reconcile(document, 1, NOW + timedelta(minutes=3))
        controller.reconcile(document, 1, NOW + timedelta(minutes=3))
        assert kube.applied[-1]["kind"] == "Job"
        assert kube.applied[-1]["metadata"]["name"] != job["metadata"]["name"]


@pytest.mark.parametrize(
    "state",
    [
        {"succeeded": 1},
        {"failed": 1, "conditions": [{"type": "FailureTarget", "status": "True"}]},
        {"conditions": [{"type": "Complete", "status": "False"}]},
    ],
)
def test_acceptance_waits_for_terminal_job_condition(state):
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 1, NOW)
        kube.get("Job", "pig", kube.applied[-1]["metadata"]["name"])["status"] = state
        controller.reconcile(document, 1, NOW + timedelta(minutes=3))
    assert "acceptedJob" not in document["status"]
    assert "acceptanceNextCheck" not in document["status"]
    assert document["status"].get("acceptanceAttempt", 0) == 0


def test_migration_creation_requires_persisted_boundary(monkeypatch):
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    with client_for(manifest(), manifest("2.0.0")) as client:
        upgrade_to_phase(kube, document, client, "quiesce")
        before = len([r for r in kube.applied if r["kind"] == "Job"])
        persist = kube.status

        def conflict(deployment, status):
            assert status["migrationMayHaveRun"] is True
            raise KubeError(409, "status write conflict")

        monkeypatch.setattr(kube, "status", conflict)
        with pytest.raises(KubeError):
            Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
        assert document["status"]["phase"] == "quiesce"
        assert document["status"]["migrationMayHaveRun"] is False
        assert len([r for r in kube.applied if r["kind"] == "Job"]) == before
        monkeypatch.setattr(kube, "status", persist)
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
        assert document["status"]["phase"] == "migration"
        assert document["status"]["migrationMayHaveRun"] is True
        assert len([r for r in kube.applied if r["kind"] == "Job"]) == before
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
        assert len([r for r in kube.applied if r["kind"] == "Job"]) == before + 1


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
                "deploymentUID": document["metadata"]["uid"],
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


@pytest.mark.parametrize("configuration_reset", [False, True])
def test_failed_target_cannot_restore_old_analyzer_after_incompatible_migration(configuration_reset):
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
        if configuration_reset:
            kube.get("Secret", "pig", "pig-credentials")["metadata"]["resourceVersion"] = "2"
            controller.reconcile(document, 1, NOW)
            assert document["status"]["phase"] == "preflight"
            assert document["status"]["migrationMayHaveRun"] is True
        before = len(kube.applied)
        document["spec"]["release"] = {"pinnedVersion": "1.0.0"}
        controller.reconcile(document, 1, NOW)
        assert reason(document) == "RollbackUnsupported"
        assert document["status"]["targetVersion"] == "2.0.0"
        assert len(kube.applied) == before


@pytest.mark.parametrize("repair_checkpoint", [False, True])
@pytest.mark.parametrize("legacy_boundary", [False, True])
def test_unfinished_repair_cannot_authorize_rollback_of_an_earlier_migration(repair_checkpoint, legacy_boundary):
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        original = select_release(client, CATALOG)
        reconcile_to_ready(kube, document, client)
    migrated = manifest("2.0.0", schemaFrom=[1], schemaTo=2)
    with client_for(manifest(), migrated) as client:
        upgrade_to_phase(kube, document, client, "verify")
    if legacy_boundary:
        document["status"].pop("migrationDigest")
    migration_digest = document["status"].get("migrationDigest")
    repair = {**manifest("3.0.0"), "rollbackTo": [original.digest]}
    document["spec"]["release"] = {"pinnedVersion": "3.0.0"}
    with client_for(manifest(), migrated, repair) as client:
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        controller.reconcile(document, 1, NOW)
        assert document["status"]["targetVersion"] == "3.0.0"
        assert document["status"]["phase"] == "preflight"
        if repair_checkpoint:
            for _ in range(4):
                controller.reconcile(document, 1, NOW)
                kube.finish_jobs()
                if document["status"]["phase"] == "migration":
                    break
            assert document["status"]["phase"] == "migration"
        assert document["status"].get("migrationDigest") == migration_digest
        document["spec"]["release"] = {"pinnedVersion": "1.0.0"}
        before = len(kube.applied)
        controller.reconcile(document, 1, NOW)
        assert reason(document) == "RollbackUnsupported"
        assert document["status"]["targetVersion"] == "3.0.0"
        assert len(kube.applied) == before


@pytest.mark.parametrize("unfinished", [False, True])
def test_compatible_rollback_runs_fresh_preflight_instead_of_reusing_old_success(unfinished):
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        original = select_release(client, CATALOG)
        reconcile_to_ready(kube, document, client)
    target = {**manifest("2.0.0"), "rollbackTo": [original.digest]}
    with client_for(manifest(), target) as client:
        for _ in range(24):
            Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
            kube.finish_jobs()
            if unfinished and document["status"]["phase"] == "verify":
                break
            if document["status"].get("currentVersion") == "2.0.0":
                break
        assert document["status"]["currentVersion"] == ("1.0.0" if unfinished else "2.0.0")
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


@pytest.mark.parametrize("name", ["acme.prod", "123acme", "-acme", "acme-", "Acme", "a" * 55])
def test_invalid_child_service_name_blocks_before_external_checks(name: str) -> None:
    kube, document = FakeKube(), deployment()
    document["metadata"]["name"] = name
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected catalog fetch"))) as client:
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
    assert reason(document) == "InvalidDeploymentName"
    assert not kube.applied


@pytest.mark.parametrize("name", ["a", "acme-1", "a" * 54])
def test_valid_child_service_name_can_start_installation(name: str) -> None:
    kube, document = FakeKube(), deployment()
    document["metadata"]["name"] = name
    with client_for(manifest()) as client:
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
    assert document["status"]["targetVersion"] == "1.0.0"


@pytest.mark.parametrize(
    "job_state",
    [
        None,
        {},
        {"active": 1},
        {"succeeded": 1},
        {"failed": 1, "conditions": [{"type": "FailureTarget", "status": "True"}]},
        {"conditions": [{"type": "Complete", "status": "False"}]},
        {"conditions": [{"type": "Complete", "status": "True"}]},
        {"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]},
    ],
)
def test_configuration_rotation_at_migration_checkpoint(job_state: dict | None) -> None:
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    with client_for(manifest(), manifest("2.0.0")) as client:
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        for _ in range(12):
            controller.reconcile(document, 1, NOW)
            kube.finish_jobs()
            if document["status"]["phase"] == "migration":
                break
        assert document["status"]["phase"] == "migration"
        assert kube.get("Deployment", "pig", "acme-analyzer")["spec"]["replicas"] == 0
        previous_transition = document["status"]["transitionID"]
        if job_state is not None:
            controller.reconcile(document, 1, NOW)
            assert kube.applied[-1]["kind"] == "Job"
            job = kube.get("Job", "pig", kube.applied[-1]["metadata"]["name"])
            job["status"] = job_state
        kube.get("Secret", "pig", "pig-credentials")["metadata"]["resourceVersion"] = "2"
        controller.reconcile(document, 1, NOW)
        if job_state is not None and not any(
            c["type"] in {"Complete", "Failed"} and c["status"] == "True" for c in job_state.get("conditions", [])
        ):
            assert reason(document) == "WaitingForMigration"
            assert document["status"]["transitionID"] == previous_transition
            return
        assert document["status"]["phase"] == "preflight"
        assert document["status"]["transitionID"] != previous_transition
        for _ in range(24):
            controller.reconcile(document, 1, NOW)
            kube.finish_jobs()
            if document["status"].get("currentVersion") == "2.0.0":
                break
        assert document["status"]["currentVersion"] == "2.0.0"
        assert kube.get("Deployment", "pig", "acme-analyzer")["spec"]["replicas"] == 1


@pytest.mark.parametrize("action", ["retry", "repair", "rotate"])
@pytest.mark.parametrize("outcome", ["Failed", "Complete", "deleted"])
def test_terminal_migration_waits_for_terminating_pods(action, outcome):
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    with client_for(manifest(), manifest("2.0.0")) as client:
        upgrade_to_phase(kube, document, client, "migration")
        Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor").reconcile(document, 1, NOW)
    job = kube.get("Job", "pig", kube.applied[-1]["metadata"]["name"])
    job["status"] = {"conditions": [{"type": outcome, "status": "True"}]}
    kube.pods = [
        {
            "metadata": {
                "labels": job["spec"]["template"]["metadata"]["labels"],
                "deletionTimestamp": NOW.isoformat(),
            },
            "status": {"phase": "Running"},
        }
    ]
    if outcome == "deleted":
        del kube.documents[("Job", "pig", job["metadata"]["name"])]
    releases = [manifest(), manifest("2.0.0")]
    if action == "repair":
        releases.append(manifest("2.0.1"))
        document["spec"]["release"] = {"pinnedVersion": "2.0.1"}
    elif action == "rotate":
        kube.get("Secret", "pig", "pig-credentials")["metadata"]["resourceVersion"] = "2"
    before = len(kube.applied)
    transition = document["status"]["transitionID"]
    with client_for(*releases) as client:
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        for now in (NOW, NOW + timedelta(minutes=3)):
            controller.reconcile(document, 1, now)
            assert document["status"]["phase"] == "migration"
            assert document["status"]["targetVersion"] == "2.0.0"
            assert document["status"]["transitionID"] == transition
            assert "retryAt" not in document["status"]
            assert len(kube.applied) == before
        kube.pods[0]["status"]["phase"] = "Failed"
        controller.reconcile(document, 1, NOW + timedelta(minutes=4))
        if action == "repair":
            assert document["status"]["targetVersion"] == "2.0.1"
            assert document["status"]["phase"] == "preflight"
        elif action == "rotate":
            assert document["status"]["phase"] == "preflight"
            assert document["status"]["transitionID"] != transition
        elif outcome == "Complete":
            assert document["status"]["phase"] == "rollout"
        elif outcome == "deleted":
            assert len(kube.applied) == before + 1
            assert kube.applied[-1]["kind"] == "Job"
            assert kube.applied[-1]["metadata"]["name"] == job["metadata"]["name"]
        else:
            assert reason(document) == "MigrationBlocked"
            assert "retryAt" in document["status"]
            controller.reconcile(document, 1, NOW + timedelta(minutes=7))
            controller.reconcile(document, 1, NOW + timedelta(minutes=7))
            assert kube.applied[-1]["kind"] == "Job"
            assert kube.applied[-1]["metadata"]["name"] != job["metadata"]["name"]


@pytest.mark.parametrize("requirements", [{}, CANDIDATE_REQUIREMENTS], ids=["schema-1", "schema-2-candidate"])
def test_quiesce_waits_for_terminating_analyzer_pods_and_resumes_with_apply(requirements) -> None:
    kube, document = FakeKube(), deployment()
    with client_for(manifest()) as client:
        reconcile_to_ready(kube, document, client)
    with client_for(manifest(), manifest("2.0.0", **requirements)) as client:
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        for _ in range(12):
            controller.reconcile(document, 1, NOW)
            kube.finish_jobs()
            if document["status"]["phase"] == "quiesce":
                break
        kube.pods = [
            {
                "metadata": {
                    "deletionTimestamp": NOW.isoformat(),
                    "labels": {"app.kubernetes.io/name": "acme-analyzer", "app.kubernetes.io/component": "analyzer"},
                },
                "status": {"phase": "Running"},
            },
            {
                "metadata": {
                    "labels": {"app.kubernetes.io/name": "acme-analyzer", "app.kubernetes.io/component": "maintenance"},
                },
                "status": {"phase": "Running"},
            },
        ]
        before = len(kube.applied)
        controller.reconcile(document, 1, NOW)
        assert document["status"]["phase"] == "quiesce"
        quiesced = kube.get("Deployment", "pig", "acme-analyzer")
        assert quiesced["spec"]["replicas"] == quiesced["status"]["replicas"] == 0
        assert [resource["kind"] for resource in kube.applied[before:]] == ["Deployment"]
        kube.pods[0]["status"]["phase"] = "Succeeded"
        controller.reconcile(document, 1, NOW)
        assert document["status"]["phase"] == "migration"
        controller.reconcile(document, 1, NOW)
        migration = kube.applied[-1]
        container = migration["spec"]["template"]["spec"]["containers"][0]
        assert container["args"] == ["supervised-migrate"]
        passed_requirements = json.loads(next(v["value"] for v in container["env"] if v["name"] == "PIG_REQUIREMENTS"))
        assert passed_requirements["schemaFrom"] == [0, 1]
        assert passed_requirements["schemaTo"] == requirements.get("schemaTo", 1)
        controller.reconcile(document, 1, NOW)
        assert document["status"]["phase"] == "migration"
        assert kube.get("Deployment", "pig", "acme-analyzer")["spec"]["replicas"] == 0
        for _ in range(24):
            controller.reconcile(document, 1, NOW)
            kube.finish_jobs()
            if document["status"].get("currentVersion") == "2.0.0":
                break
        assert document["status"]["currentVersion"] == "2.0.0"
        replicas = [
            resource["spec"]["replicas"] for resource in kube.applied[before:] if resource["kind"] == "Deployment"
        ]
        assert replicas[:2] == [0, 0] and replicas[-1] == 1


def test_failed_analyzer_rollout_can_select_a_new_stable_repair() -> None:
    kube, document = FakeKube(), deployment()
    kube.rollout_failure = True
    with client_for(manifest()) as client:
        controller = Controller(kube, client, CATALOG, "pig", "pig-system", "pig-supervisor")
        for _ in range(12):
            controller.reconcile(document, 1, NOW)
            kube.finish_jobs()
            if reason(document) == "RolloutFailed":
                break
    assert reason(document) == "RolloutFailed"
    assert document["status"]["phase"] == "rollout"
    kube.rollout_failure = False
    with client_for(manifest(), manifest("1.0.1")) as client:
        reconcile_to_ready(kube, document, client)
    assert document["status"]["currentVersion"] == "1.0.1"


def test_rollout_failure_from_an_older_generation_is_not_current() -> None:
    resource = {
        "metadata": {"generation": 2},
        "spec": {"template": {"spec": {"containers": [{"image": "target"}]}}},
        "status": {
            "observedGeneration": 1,
            "conditions": [{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}],
        },
    }
    assert not Controller._deployment_ready(resource, "target")
    resource["status"]["observedGeneration"] = 2
    with pytest.raises(Blocked, match="progress deadline"):
        Controller._deployment_ready(resource, "target")


def test_quiesce_and_rollout_use_the_same_apply_owner() -> None:
    """Replica changes keep the complete owned Deployment under one apply manager."""
    document = deployment()
    spec = DeploymentSpec.model_validate(document["spec"])
    selected = SelectedRelease(Release.model_validate(manifest()), "a" * 64)
    live = analyzer_resources(document, spec, selected.release, "config")[0]
    replica_writes = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal live
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": []})
        if request.method == "PATCH":
            assert request.headers["Content-Type"] == "application/apply-patch+yaml"
            assert request.url.params["fieldManager"] == "pig-supervisor"
            assert request.url.params["force"] == "false"
            applied = json.loads(request.content)
            if applied["kind"] == "Deployment":
                live = applied
                replica_writes.append(live["spec"]["replicas"])
            else:
                return httpx.Response(200, json=applied)
        elif not request.url.path.endswith("/deployments/acme-analyzer"):
            return httpx.Response(404, json={})
        replicas = live["spec"]["replicas"]
        live["metadata"]["generation"] = 1
        live["status"] = {
            "observedGeneration": 1,
            "replicas": replicas,
            "updatedReplicas": replicas,
            "availableReplicas": replicas,
        }
        return httpx.Response(200, json=live)

    with (
        httpx.Client(base_url="https://kubernetes.example", transport=httpx.MockTransport(respond)) as api,
        client_for(manifest()) as catalog,
    ):
        kube = Kube(api)
        kube.lease_deadline = monotonic() + 60
        controller = Controller(kube, catalog, CATALOG, "pig", "pig-system", "pig-supervisor")
        status = {"phase": "quiesce", "currentDigest": selected.digest}
        controller._advance(document, spec, selected, "config", status, NOW)
        assert status["phase"] == "rollout"
        controller._advance(document, spec, selected, "config", status, NOW)
        assert status["phase"] == "verify"
    assert replica_writes == [0, 1]
    assert (
        live["spec"]["template"]
        == analyzer_resources(document, spec, selected.release, "config")[0]["spec"]["template"]
    )
