"""Small Kubernetes REST client with projected-token refresh and optimistic locking."""

from __future__ import annotations

import builtins
import re
from datetime import datetime
from pathlib import Path
from time import monotonic
from urllib.parse import quote

import httpx

GROUP = "governance.promptless.ai/v1alpha1"
PLURALS = {
    "Deployment": ("apps/v1", "deployments"),
    "Service": ("v1", "services"),
    "Ingress": ("networking.k8s.io/v1", "ingresses"),
    "Job": ("batch/v1", "jobs"),
    "ConfigMap": ("v1", "configmaps"),
    "Secret": ("v1", "secrets"),
    "ServiceAccount": ("v1", "serviceaccounts"),
    "Lease": ("coordination.k8s.io/v1", "leases"),
    "Pod": ("v1", "pods"),
    "PIGDeployment": (GROUP, "pigdeployments"),
    "HelmRelease": ("helm.toolkit.fluxcd.io/v2", "helmreleases"),
}


class KubeError(RuntimeError):
    """Expose failed operation metadata without Kubernetes response bodies or credentials."""

    def __init__(self, status: int, method: str = "", path: str = "", reason: str = "") -> None:
        operation = f"{method} {path}" if method else "API"
        super().__init__(f"Kubernetes {operation} returned {status}" + (f": {reason}" if reason else ""))
        self.status = status
        self.method = method
        self.path = path
        self.reason = reason


def resource_path(kind: str, namespace: str, name: str = "") -> str:
    version, plural = PLURALS[kind]
    prefix = "/api/" if version == "v1" else "/apis/"
    return f"{prefix}{version}/namespaces/{quote(namespace, safe='')}/{plural}" + (
        f"/{quote(name, safe='')}" if name else ""
    )


class Kube:
    def __init__(self, client: httpx.Client, token_path: Path | None = None) -> None:
        self.client = client
        self.token_path = token_path
        self.lease_deadline = 0.0
        self.lease_observations: dict[str, tuple[dict, float]] = {}

    def request(self, method: str, path: str, **kwargs) -> dict:
        lease_request = re.fullmatch(r"/apis/coordination.k8s.io/v1/namespaces/[^/]+/leases(?:/[^/]+)?", path)
        if method != "GET" and not lease_request and monotonic() >= self.lease_deadline:
            raise KubeError(409, method, path, "controller Lease expired")
        headers = kwargs.pop("headers", {})
        if self.token_path:
            headers["Authorization"] = "Bearer " + self.token_path.read_text().strip()
        response = self.client.request(method, path, headers=headers, **kwargs)
        if not response.is_success:
            raise KubeError(response.status_code, method, path)
        return response.json()

    def get(self, kind: str, namespace: str, name: str) -> dict | None:
        try:
            return self.request("GET", resource_path(kind, namespace, name))
        except KubeError as exc:
            if exc.status == 404:
                return None
            raise

    def list(self, kind: str, namespace: str, selector: str = "") -> builtins.list[dict]:
        return self.request("GET", resource_path(kind, namespace), params={"labelSelector": selector}).get("items", [])

    def apply(self, document: dict) -> dict:
        metadata = document["metadata"]
        path = resource_path(document["kind"], metadata["namespace"], metadata["name"])
        existing = self.get(document["kind"], metadata["namespace"], metadata["name"])
        if existing:
            expected = metadata.get("ownerReferences", [])
            actual = existing["metadata"].get("ownerReferences", [])
            if expected and not any(owner.get("uid") == expected[0]["uid"] for owner in actual):
                raise KubeError(409, "PATCH", path, "resource belongs to a different owner")
        return self.request(
            "PATCH",
            path,
            json=document,
            params={"fieldManager": "pig-supervisor", "force": "false"},
            headers={"Content-Type": "application/apply-patch+yaml"},
        )

    def patch(self, kind: str, namespace: str, name: str, patch: dict) -> dict:
        return self.request(
            "PATCH",
            resource_path(kind, namespace, name),
            json=patch,
            headers={
                "Content-Type": "application/strategic-merge-patch+json"
                if kind == "Deployment"
                else "application/merge-patch+json"
            },
        )

    def status(self, deployment: dict, status: dict) -> None:
        """Persist the desired status, explicitly deleting removed fields with a merge patch."""
        metadata = deployment["metadata"]
        status_patch = {key: None for key in deployment.get("status", {}) if key not in status} | status
        self.request(
            "PATCH",
            resource_path("PIGDeployment", metadata["namespace"], metadata["name"]) + "/status",
            json={"metadata": {"resourceVersion": metadata["resourceVersion"]}, "status": status_patch},
            headers={"Content-Type": "application/merge-patch+json"},
        )

    def leadership(self, namespace: str, holder: str, now: datetime) -> bool:
        """A fixed namespace Lease also prevents two separately installed supervisors acting at once."""
        name = "pig-supervisor"
        started = monotonic()
        self.lease_deadline = 0.0
        lease = self.get("Lease", namespace, name)
        spec = {
            "holderIdentity": holder,
            "leaseDurationSeconds": 300,
            "renewTime": now.isoformat().replace("+00:00", "Z"),
        }
        if lease is None:
            try:
                self.request(
                    "POST",
                    resource_path("Lease", namespace),
                    json={
                        "apiVersion": "coordination.k8s.io/v1",
                        "kind": "Lease",
                        "metadata": {"name": name, "namespace": namespace},
                        "spec": spec,
                    },
                )
                self.lease_deadline = started + 270
                return True
            except KubeError as exc:
                if exc.status == 409:
                    return False
                raise
        previous = lease.get("spec", {})
        # Observe renewals locally: another node's wall clock cannot establish expiry.
        # https://pkg.go.dev/k8s.io/client-go/tools/leaderelection
        record = {"uid": lease["metadata"].get("uid"), "spec": previous}
        observed, observed_at = self.lease_observations.get(namespace, (None, 0.0))
        if record != observed:
            observed_at = monotonic()
            self.lease_observations[namespace] = (record, observed_at)
        if previous.get("holderIdentity") != holder and monotonic() < observed_at + previous.get(
            "leaseDurationSeconds", 300
        ):
            return False
        try:
            self.patch(
                "Lease",
                namespace,
                name,
                {"metadata": {"resourceVersion": lease["metadata"]["resourceVersion"]}, "spec": spec},
            )
            self.lease_deadline = started + 270
            return True
        except KubeError as exc:
            if exc.status == 409:
                return False
            raise
