"""Restartable release coordination with explicit safe checkpoints and ownership checks."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
import yaml
from packaging.version import Version
from pydantic import ValidationError

from .catalog import SelectedRelease, canonical_digest, select_release, verified_bytes
from .crd import merge_crd
from .kube import Kube, KubeError
from .models import DeploymentSpec, Release, stable_version, validation_details
from .workloads import analyzer_resources, job_resource, secret_refs

CAPABILITIES = frozenset({"native-storage-v1", "migration-ledger-v1", "controller-self-update-v1", "crd-update-v1"})
PHASES = ("preflight", "quiesce", "migration", "rollout", "verify", "supervisor", "complete")


def job_outcome(job: dict | None) -> str | None:
    """Wait for terminal Job conditions, including failures that never created a Pod."""
    # Pod counters and FailureTarget can precede termination of the remaining Pods.
    # https://kubernetes.io/docs/concepts/workloads/controllers/job/#terminal-job-conditions
    for entry in (job or {}).get("status", {}).get("conditions", []):
        if entry.get("type") in {"Complete", "Failed"} and entry.get("status") == "True":
            return entry["type"]
    return None


class Blocked(RuntimeError):
    """A bounded, operator-actionable reason that never embeds credential contents."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def condition(status: dict, name: str, truth: bool, reason: str, message: str, generation: int, now: datetime) -> None:
    value = {
        "type": name,
        "status": "True" if truth else "False",
        "reason": reason,
        "message": message,
        "observedGeneration": generation,
        "lastTransitionTime": now.isoformat(),
    }
    previous = next((entry for entry in status.get("conditions", []) if entry["type"] == name), None)
    if previous and previous["status"] == value["status"] and previous.get("reason") == reason:
        value["lastTransitionTime"] = previous["lastTransitionTime"]
    status["conditions"] = [entry for entry in status.get("conditions", []) if entry["type"] != name] + [value]


def recovery_check(spec: DeploymentSpec, selected: SelectedRelease, document: dict | None, now: datetime) -> None:
    """Bind a customer confirmation to this installation and exact immutable transition."""
    hours = selected.release.requirements.recovery_max_age_hours
    if hours is None:
        return
    if document is None:
        raise Blocked(
            "RecoveryConfirmationRequired", "Supply the recovery ConfigMap declared in spec.release.confirmation."
        )
    data = document.get("data", {})
    try:
        confirmed = datetime.fromisoformat(data["confirmedAt"])
    except (KeyError, ValueError):
        raise Blocked("RecoveryConfirmationInvalid", "confirmedAt must be a timestamp with a timezone.") from None
    if (
        confirmed.tzinfo is None
        or confirmed > now
        or confirmed < now - timedelta(hours=hours)
        or data.get("releaseDigest") != "sha256:" + selected.digest
        or data.get("deploymentID") != spec.hosted.deployment_id
        or not data.get("postgresRecoveryPoint")
        or not data.get("objectRecoveryPoint")
    ):
        raise Blocked(
            "RecoveryConfirmationInvalid",
            "Refresh the recovery points and bind the confirmation to this deployment and target digest.",
        )


def capacity_check(spec: DeploymentSpec, selected: SelectedRelease, document: dict | None, now: datetime) -> None:
    """Require an explicit acknowledgement for capacity that cannot be measured by this controller."""
    requirements = selected.release.requirements.operator_capacity_requirements
    if not requirements:
        return
    data = (document or {}).get("data", {})
    try:
        confirmed = datetime.fromisoformat(data["capacityConfirmedAt"])
    except (KeyError, ValueError):
        raise Blocked(
            "CapacityConfirmationRequired",
            "Review the target release operatorCapacityRequirements, make required Terraform changes, then provide a release-specific capacity acknowledgement.",
        ) from None
    if (
        confirmed.tzinfo is None
        or confirmed > now
        or confirmed < now - timedelta(hours=selected.release.requirements.capacity_confirmation_max_age_hours)
        or data.get("releaseDigest") != "sha256:" + selected.digest
        or data.get("deploymentID") != spec.hosted.deployment_id
        or data.get("capacityRequirementsDigest") != "sha256:" + canonical_digest(requirements)
        or not data.get("capacityEvidence")
    ):
        raise Blocked(
            "CapacityConfirmationInvalid",
            "Refresh capacityEvidence and capacityConfirmedAt for this deployment, releaseDigest, and capacityRequirementsDigest. This is operator acknowledgement, not live capacity verification.",
        )


class Controller:
    def __init__(
        self,
        kube: Kube,
        catalog: httpx.Client,
        catalog_url: str,
        namespace: str,
        system_namespace: str,
        supervisor_name: str,
    ) -> None:
        self.kube, self.catalog, self.catalog_url = kube, catalog, catalog_url
        self.namespace, self.system_namespace, self.supervisor_name = namespace, system_namespace, supervisor_name

    def reconcile(self, deployment: dict, population: int, now: datetime) -> None:
        """Perform at most one durable phase change; all operations are safe to repeat."""
        status = deepcopy(deployment.get("status", {}))
        generation = deployment["metadata"]["generation"]
        try:
            if population != 1:
                raise Blocked(
                    "MultipleDeployments",
                    "One PIGDeployment per watched namespace owns this supervisor's release policy.",
                )
            name = deployment["metadata"]["name"]
            if len(name) > 54 or re.fullmatch(r"[a-z](?:[-a-z0-9]*[a-z0-9])?", name) is None:
                raise Blocked(
                    "InvalidDeploymentName",
                    "Use at most 54 lowercase letters, digits, or hyphens, starting with a letter and ending with a letter or digit.",
                )
            spec = DeploymentSpec.model_validate(deployment["spec"])
            self._check_handoff()
            config_hash = self._configuration_hash(spec)
            if (
                spec.release.paused
                and status.get("targetDigest")
                and status["targetDigest"] != status.get("currentDigest")
            ):
                if status.get("currentManifest") and status.get("migrationMayHaveRun") is False:
                    # Cancel an unstarted upgrade before resolving its catalog entry. The
                    # installed release can still rotate credentials while offline.
                    status.update(targetDigest=None, targetVersion=None, phase="complete")
                    status.pop("retryAt", None)
                else:
                    # Phase alone is insufficient: rotation or repair can reset it after
                    # a migration. Older status without this marker is also unsafe.
                    raise Blocked(
                        "Paused",
                        "Update paused; resume with spec.release.paused=false to finish the schema transition before rotating configuration.",
                    )
            if status.get("targetDigest") == status.get("currentDigest") and status.get("currentManifest"):
                selected = SelectedRelease(Release.model_validate(status["currentManifest"]), status["currentDigest"])
            elif status.get("targetDigest"):
                # A channel advance cannot change a transaction already in progress.
                selected = select_release(self.catalog, self.catalog_url, status["targetVersion"])
                if selected.digest != status["targetDigest"]:
                    raise Blocked(
                        "ImmutableReleaseChanged",
                        "The catalog changed an existing release; restore its original digest.",
                    )
            elif spec.release.paused:
                if not status.get("currentManifest"):
                    raise Blocked("Paused", "Unpause to install a release.")
                selected = SelectedRelease(Release.model_validate(status["currentManifest"]), status["currentDigest"])
            else:
                selected = select_release(self.catalog, self.catalog_url, spec.release.pinned_version)
            current_digest = status.get("currentDigest")
            if status.get("targetDigest") and not spec.release.paused:
                requested = select_release(self.catalog, self.catalog_url, spec.release.pinned_version)
                failed = any(c["type"] == "Blocked" and c["status"] == "True" for c in status.get("conditions", []))
                replacement = requested.digest != selected.digest and (
                    bool(spec.release.pinned_version)
                    or (failed and stable_version(requested.release.version) > stable_version(selected.release.version))
                )
                if replacement:
                    if status["phase"] == "migration":
                        active = job_resource(
                            deployment,
                            spec,
                            selected.release,
                            selected.digest,
                            status["transitionConfigurationHash"],
                            "migration",
                            status.get("attempt", 0),
                            transition=status.get("transitionID", ""),
                        )
                        job = self.kube.get("Job", self.namespace, active["metadata"]["name"])
                        if job and (job_outcome(job) is None or self._migration_pods_running(job)):
                            raise Blocked(
                                "WaitingForMigration",
                                "The current migration must finish before selecting the requested repair release.",
                            )
                    if (
                        status.get("migrationMayHaveRun", True)
                        and stable_version(requested.release.version) < stable_version(selected.release.version)
                        and (
                            requested.digest not in selected.release.rollback_to
                            or requested.release.requirements.schema_to != selected.release.requirements.schema_to
                        )
                    ):
                        raise Blocked(
                            "RollbackUnsupported",
                            "The in-progress release may have changed the schema. Select its declared compatible rollback or a forward repair release.",
                        )
                    selected = requested
                    # A repair cannot prove the schema history of an older controller's status.
                    status.setdefault("migrationMayHaveRun", True)
                    status.update(targetDigest=None, targetVersion=None, phase="preflight")
            if (
                selected.digest == current_digest
                and not status.get("targetDigest")
                and status.get("phase") == "complete"
                and status.get("configurationHash") == config_hash
            ):
                for resource in analyzer_resources(deployment, spec, selected.release, config_hash):
                    self.kube.apply(resource)
                if not self._analyzer_ready(deployment, selected.release, config_hash):
                    raise Blocked("AnalyzerNotReady", "Waiting for analyzer rollout or Secret rotation.")
                status["configurationHash"] = config_hash
                self._acceptance(deployment, spec, selected, config_hash, status, now)
                self._conditions(status, generation, now, ready=True, updating=False)
            else:
                if not status.get("targetDigest"):
                    self._requirements(spec, selected)
                    recovery = (
                        self.kube.get("ConfigMap", self.namespace, spec.release.confirmation.config_map_ref)
                        if spec.release.confirmation
                        else None
                    )
                    if selected.digest != current_digest:
                        recovery_check(spec, selected, recovery, now)
                        capacity_check(spec, selected, recovery, now)
                    if (
                        selected.digest != current_digest
                        and selected.release.requirements.operator_capacity_requirements
                    ):
                        condition(
                            status,
                            "CapacityAcknowledged",
                            True,
                            "OperatorConfirmed",
                            "The operator acknowledged this release's capacity prerequisites; this is not live free-capacity verification.",
                            generation,
                            now,
                        )
                    if current_digest and stable_version(selected.release.version) < stable_version(
                        status["currentVersion"]
                    ):
                        current = Release.model_validate(status["currentManifest"])
                        if (
                            selected.digest not in current.rollback_to
                            or selected.release.requirements.schema_to != current.requirements.schema_to
                        ):
                            raise Blocked(
                                "RollbackUnsupported", "This schema requires a compatible forward repair release."
                            )
                    status.update(
                        targetDigest=selected.digest,
                        targetVersion=selected.release.version,
                        phase="preflight",
                        attempt=0,
                        transitionConfigurationHash=config_hash,
                        transitionID=str(uuid4()),
                    )
                    status.setdefault("migrationMayHaveRun", False)
                    status.pop("retryAt", None)
                    self._conditions(status, generation, now, ready=False, updating=True)
                else:
                    phase = status["phase"]
                    if config_hash != status["transitionConfigurationHash"]:
                        # Never replace a running migration because a Secret changed. Wait for its transaction to finish.
                        if phase == "migration" and not self._job(
                            deployment,
                            spec,
                            selected,
                            status["transitionConfigurationHash"],
                            phase,
                            status,
                            now,
                            start=False,
                        ):
                            raise Blocked(
                                "WaitingForMigration",
                                "The active migration must finish before new configuration is checked.",
                            )
                        status.update(
                            phase="preflight",
                            attempt=0,
                            transitionConfigurationHash=config_hash,
                            transitionID=str(uuid4()),
                        )
                        status.pop("retryAt", None)
                    else:
                        self._advance(deployment, spec, selected, config_hash, status, now)
                    self._conditions(
                        status,
                        generation,
                        now,
                        ready=status.get("phase") == "complete",
                        updating=status.get("phase") != "complete",
                    )
            status["observedGeneration"] = generation
        except Blocked as exc:
            condition(status, "Blocked", True, exc.reason, str(exc), generation, now)
            condition(status, "Ready", False, exc.reason, str(exc), generation, now)
            condition(status, "Updating", bool(status.get("targetDigest")), exc.reason, str(exc), generation, now)
        except ValidationError as exc:
            message = f"Invalid deployment or release configuration: {validation_details(exc)}"
            condition(
                status,
                "Blocked",
                True,
                "InvalidConfiguration",
                message,
                generation,
                now,
            )
            condition(status, "Ready", False, "InvalidConfiguration", message, generation, now)
        if status != deployment.get("status", {}):
            self.kube.status(deployment, status)

    def _conditions(self, status: dict, generation: int, now: datetime, *, ready: bool, updating: bool) -> None:
        condition(
            status,
            "Ready",
            ready,
            "ReleaseVerified" if ready else "TransitionInProgress",
            "Release installed and dependency checks passed; see Acceptance for end-to-end evidence."
            if ready
            else "Release transition is in progress.",
            generation,
            now,
        )
        condition(
            status,
            "Updating",
            updating,
            "TransitionInProgress" if updating else "Idle",
            "A release transition is active." if updating else "No release transition is active.",
            generation,
            now,
        )
        condition(
            status,
            "Blocked",
            False,
            "ChecksPassed",
            "No customer action is required by the current checks.",
            generation,
            now,
        )

    def _check_handoff(self) -> None:
        try:
            releases = self.kube.list("HelmRelease", self.system_namespace)
        except KubeError as exc:
            if exc.status == 404:
                return
            raise
        for release in releases:
            spec = release.get("spec", {})
            release_name = spec.get("releaseName") or release["metadata"]["name"]
            if release_name == self.supervisor_name and not spec.get("suspend", False):
                raise Blocked("FluxHandoffRequired", "Suspend the bootstrap HelmRelease before PIG manages releases.")

    def _configuration_hash(self, spec: DeploymentSpec) -> str:
        account = self.kube.get("ServiceAccount", self.namespace, spec.service_account_name)
        if account is None:
            raise Blocked(
                "ServiceAccountMissing", "Deliver the customer-owned analyzer ServiceAccount before installation."
            )
        revisions = {"serviceAccount/" + spec.service_account_name: account["metadata"]["resourceVersion"]}
        for ref in secret_refs(spec):
            secret = self.kube.get("Secret", self.namespace, ref.name)
            if secret is None or not secret.get("data", {}).get(ref.key):
                raise Blocked("SecretKeyMissing", f"Deliver key {ref.key} in Secret {ref.name}.")
            revisions[ref.name] = secret["metadata"]["resourceVersion"]
        ca = spec.storage.postgres.ca_config_map_ref
        if ca:
            document = self.kube.get("ConfigMap", self.namespace, ca.name)
            if document is None or ca.key not in document.get("data", {}):
                raise Blocked("PostgresCAMissing", "Deliver the PostgreSQL CA ConfigMap before installation.")
            revisions["ca/" + ca.name] = document["metadata"]["resourceVersion"]
        settings = spec.model_dump(by_alias=True, exclude={"release"})
        return canonical_digest([settings, revisions])

    def _requirements(self, spec: DeploymentSpec, selected: SelectedRelease) -> None:
        requirements = selected.release.requirements
        if set(requirements.capabilities) - CAPABILITIES:
            raise Blocked(
                "RBACUpgradeRequired",
                "This release needs capabilities outside the bootstrap RBAC; review and apply an explicit bootstrap upgrade.",
            )
        version = self.kube.request("GET", "/version")["gitVersion"].lstrip("v").split("-")[0]
        if Version(version) < Version(requirements.kubernetes_min):
            raise Blocked(
                "KubernetesUpgradeRequired", "Upgrade the existing cluster through customer infrastructure management."
            )
        backend = "s3" if spec.storage.s3 else "azureBlob" if spec.storage.azure_blob else "gcs"
        if backend not in requirements.storage_backends:
            raise Blocked(
                "StorageBackendUnsupported", "The target release does not support the configured native backend."
            )

    def _advance(
        self,
        deployment: dict,
        spec: DeploymentSpec,
        selected: SelectedRelease,
        config_hash: str,
        status: dict,
        now: datetime,
    ) -> None:
        phase = status["phase"]
        if phase in ("preflight", "migration", "verify"):
            if phase == "migration":
                document = job_resource(
                    deployment,
                    spec,
                    selected.release,
                    selected.digest,
                    config_hash,
                    phase,
                    status.get("attempt", 0),
                    transition=status.get("transitionID", ""),
                )
                if self.kube.get("Job", self.namespace, document["metadata"]["name"]) is None:
                    recovery = (
                        self.kube.get("ConfigMap", self.namespace, spec.release.confirmation.config_map_ref)
                        if spec.release.confirmation
                        else None
                    )
                    recovery_check(spec, selected, recovery, now)
                    capacity_check(spec, selected, recovery, now)
            if not self._job(deployment, spec, selected, config_hash, phase, status, now):
                return
        elif phase == "quiesce":
            name = deployment["metadata"]["name"] + "-analyzer"
            resource = self.kube.get("Deployment", self.namespace, name)
            if resource:
                if deployment["metadata"]["uid"] not in {
                    ref["uid"] for ref in resource["metadata"].get("ownerReferences", [])
                }:
                    raise Blocked("OwnershipConflict", "The analyzer Deployment is not owned by this PIGDeployment.")
                # Apply the complete desired Deployment so replica ownership stays with the rollout manager.
                # https://kubernetes.io/docs/reference/using-api/server-side-apply/#field-management
                quiesced = analyzer_resources(deployment, spec, selected.release, config_hash)[0]
                quiesced["spec"]["replicas"] = 0
                resource = self.kube.apply(quiesced)
                state = resource.get("status", {})
                if state.get("observedGeneration", 0) < resource["metadata"].get("generation", 1) or state.get(
                    "replicas", 0
                ):
                    return
            # Replica counts exclude terminating Pods, which can still be using the database.
            # https://github.com/kubernetes/kubernetes/blob/v1.32.0/pkg/controller/deployment/recreate.go#L89-L116
            selector = f"app.kubernetes.io/name={name},app.kubernetes.io/component=analyzer"
            if any(
                pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                for pod in self.kube.list("Pod", self.namespace, selector)
            ):
                return
        elif phase == "rollout":
            for resource in analyzer_resources(deployment, spec, selected.release, config_hash):
                self.kube.apply(resource)
            if not self._analyzer_ready(deployment, selected.release, config_hash):
                return
        elif phase == "supervisor":
            if selected.digest != status.get("currentDigest"):
                self._self_update(selected)
            controller = self.kube.get("Deployment", self.system_namespace, self.supervisor_name)
            if not controller or not self._deployment_ready(controller, selected.release.supervisor_image):
                return
        elif phase == "complete":
            status.update(
                currentDigest=selected.digest,
                currentVersion=selected.release.version,
                currentManifest=selected.release.model_dump(by_alias=True),
                configurationHash=config_hash,
                targetDigest=None,
                targetVersion=None,
                attempt=0,
                migrationMayHaveRun=False,
            )
            return
        # Configuration and credential rotation do not repeat a release migration.
        next_phase = (
            "rollout"
            if phase == "quiesce" and selected.digest == status.get("currentDigest")
            else PHASES[PHASES.index(phase) + 1]
        )
        status.update(phase=next_phase, attempt=0)
        if next_phase == "migration":
            # Persist the hazard before a later reconcile can create a migration Job.
            # Keep it through configuration resets and repair-release selection.
            status["migrationMayHaveRun"] = True

    def _job(
        self,
        deployment: dict,
        spec: DeploymentSpec,
        selected: SelectedRelease,
        config_hash: str,
        phase: str,
        status: dict,
        now: datetime,
        *,
        start: bool = True,
    ) -> bool:
        """Run a phase Job, or observe whether no transaction remains when start is false."""
        resource = job_resource(
            deployment,
            spec,
            selected.release,
            selected.digest,
            config_hash,
            phase,
            status.get("attempt", 0),
            transition=status.get("transitionID", ""),
        )
        name = resource["metadata"]["name"]
        job = self.kube.get("Job", self.namespace, name)
        if job is None:
            if start:
                self.kube.apply(resource)
            return not start
        outcome = job_outcome(job)
        if phase == "migration" and outcome and self._migration_pods_running(job):
            return False
        if outcome == "Complete":
            return True
        if outcome == "Failed":
            if not start:
                return True  # The old transaction has terminated; new credentials may be validated.
            retry_at = datetime.fromisoformat(status.get("retryAt", now.isoformat()))
            if "retryAt" not in status:
                status["retryAt"] = (now + timedelta(minutes=2)).isoformat()
            elif now >= retry_at and start:
                # Failed transactions roll back. Retry after external fixes with a new immutable Job.
                status["attempt"] = status.get("attempt", 0) + 1
                status.pop("retryAt", None)
            reason = (
                "DependencyCheckFailed"
                if phase == "preflight"
                else "MigrationBlocked"
                if phase == "migration"
                else "VerificationBlocked"
            )
            raise Blocked(
                reason,
                f"Inspect Job {name}; correct infrastructure through Terraform or deliver the required configuration. Checks retry automatically.",
            )
        return False

    def _migration_pods_running(self, job: dict) -> bool:
        # Kubernetes 1.30 can report a terminal Job condition before its Pods exit.
        selector = f"governance.promptless.ai/job={job['metadata']['name']}"
        return any(
            pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
            for pod in self.kube.list("Pod", self.namespace, selector)
        )

    @staticmethod
    def _deployment_ready(resource: dict, image: str) -> bool:
        """Check the observed rollout and surface its deadline failure for repair selection."""
        state = resource.get("status", {})
        if resource["spec"]["template"]["spec"]["containers"][0]["image"] != image or state.get(
            "observedGeneration", 0
        ) < resource["metadata"].get("generation", 1):
            return False
        if any(
            entry.get("type") == "Progressing"
            and entry.get("status") == "False"
            and entry.get("reason") == "ProgressDeadlineExceeded"
            for entry in state.get("conditions", [])
        ):
            raise Blocked(
                "RolloutFailed",
                "A Deployment exceeded its progress deadline. Inspect its Pods and correct the failure or select a compatible repair release.",
            )
        return (
            state.get("updatedReplicas", 0) == 1
            and state.get("availableReplicas", 0) == 1
            and state.get("replicas", 0) == 1
        )

    def _analyzer_ready(self, deployment: dict, release: Release, config_hash: str) -> bool:
        resource = self.kube.get("Deployment", self.namespace, deployment["metadata"]["name"] + "-analyzer")
        return bool(
            resource
            and self._deployment_ready(resource, release.analyzer_image)
            and resource["spec"]["template"]["metadata"]["annotations"].get("governance.promptless.ai/config-hash")
            == config_hash
        )

    def _self_update(self, selected: SelectedRelease) -> None:
        if selected.release.crd:
            crd = yaml.safe_load(verified_bytes(self.catalog, selected.release.crd))
            if (
                crd.get("kind") != "CustomResourceDefinition"
                or crd.get("metadata", {}).get("name") != "pigdeployments.governance.promptless.ai"
                or crd.get("spec", {}).get("scope") != "Namespaced"
                or crd.get("spec", {}).get("conversion", {}).get("strategy", "None") != "None"
            ):
                raise Blocked("CRDUpgradeUnsupported", "The release CRD exceeds the granted bootstrap contract.")
            path = "/apis/apiextensions.k8s.io/v1/customresourcedefinitions/pigdeployments.governance.promptless.ai"
            installed = self.kube.request("GET", path)
            merged = merge_crd(installed["spec"], crd["spec"])
            if merged is None:
                raise Blocked(
                    "CRDUpgradeUnsupported",
                    "The shared CRD change is not additive. Review an explicit bootstrap upgrade across every watched namespace.",
                )
            # A resourceVersion precondition prevents concurrent namespace controllers
            # from overwriting each other's shared CRD. Merge patch avoids taking Helm fields.
            # https://kubernetes.io/docs/reference/using-api/api-concepts/#patch-operations
            if merged != installed["spec"]:
                self.kube.request(
                    "PATCH",
                    path,
                    json={"metadata": {"resourceVersion": installed["metadata"]["resourceVersion"]}, "spec": merged},
                    headers={"Content-Type": "application/merge-patch+json"},
                )
        self.kube.patch(
            "Deployment",
            self.system_namespace,
            self.supervisor_name,
            {
                "spec": {
                    "template": {
                        "spec": {"containers": [{"name": "supervisor", "image": selected.release.supervisor_image}]}
                    }
                }
            },
        )

    def _acceptance(
        self,
        deployment: dict,
        spec: DeploymentSpec,
        selected: SelectedRelease,
        config_hash: str,
        status: dict,
        now: datetime,
    ) -> None:
        # Acceptance is evidence from the real host pipeline, not a synthetic model call.
        attempt = status.get("acceptanceAttempt", 0)
        document = job_resource(deployment, spec, selected.release, selected.digest, config_hash, "acceptance", attempt)
        job = self.kube.get("Job", self.namespace, document["metadata"]["name"])
        if job is None:
            self.kube.apply(document)
        outcome = job_outcome(job)
        accepted = outcome == "Complete"
        terminal = outcome is not None
        if terminal:
            next_check = status.get("acceptanceNextCheck")
            if next_check is None:
                status["acceptanceNextCheck"] = (now + timedelta(seconds=3600 if accepted else 120)).isoformat()
            elif now >= datetime.fromisoformat(next_check):
                status["acceptanceAttempt"] = attempt + 1
                status.pop("acceptanceNextCheck", None)
        if accepted:
            status["acceptedConfigurationHash"] = config_hash
            status["acceptedReleaseDigest"] = selected.digest
            if status.get("acceptedJob") != document["metadata"]["name"]:
                status["lastAcceptanceAt"] = job.get("status", {}).get("completionTime", now.isoformat())
                status["acceptedJob"] = document["metadata"]["name"]
        # A successful check is evidence, not a claim that every future trace has succeeded.
        accepted = accepted or (
            status.get("acceptedConfigurationHash") == config_hash
            and status.get("acceptedReleaseDigest") == selected.digest
        )
        condition(
            status,
            "Acceptance",
            accepted,
            "CanonicalSuccess" if accepted else "AwaitingCanonicalAcceptance",
            "An enrolled host has a readable canonical object, succeeded analysis, and hosted acknowledgement."
            if accepted
            else "Enroll a host, ingest a real trace, and wait for analysis and Dashboard synchronization.",
            deployment["metadata"]["generation"],
            now,
        )
