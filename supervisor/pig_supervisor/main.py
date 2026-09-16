"""In-cluster supervisor process; reconciliation failures are bounded status updates."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import ssl
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import httpx
from pydantic import ValidationError

from .catalog import CatalogError
from .controller import Controller, condition
from .kube import Kube, KubeError
from .models import validation_details

logger = logging.getLogger("pig-supervisor")


def describe_failure(error: httpx.HTTPError | CatalogError | ValidationError | KubeError) -> str:
    """Describe dependency failures without logging response bodies, URL credentials, or rejected values."""
    if isinstance(error, ValidationError):
        return f"ValidationError: {validation_details(error)}"
    if isinstance(error, httpx.HTTPError):
        request = error.request
        operation = f"{request.method} {request.url.host}"
        if isinstance(error, httpx.HTTPStatusError):
            return f"HTTPStatusError: {operation} returned {error.response.status_code}"
        return f"{type(error).__name__}: {operation}"
    return f"{type(error).__name__}: {error}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=version("pig-supervisor"))
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    namespace = os.environ["WATCH_NAMESPACE"]
    system_namespace = os.environ["POD_NAMESPACE"]
    holder = os.environ["POD_UID"]
    name = os.environ.get("SUPERVISOR_NAME", "pig-supervisor")
    token_root = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    context = ssl.create_default_context(cafile=str(token_root / "ca.crt"))
    stopped = False

    def stop(signum: int, frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (
        httpx.Client(
            base_url="https://kubernetes.default.svc", verify=context, timeout=15, follow_redirects=False
        ) as api,
        httpx.Client(timeout=15, follow_redirects=False) as catalog,
    ):
        kube = Kube(api, token_root / "token")
        controller = Controller(kube, catalog, os.environ["RELEASE_CATALOG_URL"], namespace, system_namespace, name)
        while not stopped:
            now = datetime.now(UTC)
            try:
                if kube.leadership(namespace, holder, now):
                    deployments = kube.list("PIGDeployment", namespace)
                    for deployment in deployments:
                        try:
                            controller.reconcile(deployment, len(deployments), now)
                        except (httpx.HTTPError, CatalogError, ValidationError, KubeError) as exc:
                            reason = (
                                "KubernetesConflict"
                                if isinstance(exc, KubeError) and exc.status == 409
                                else "DependencyUnavailable"
                            )
                            logger.warning("Reconciliation blocked: %s", describe_failure(exc))
                            status = dict(deployment.get("status", {}))
                            for key, truth in (("Ready", False), ("Blocked", True)):
                                condition(
                                    status,
                                    key,
                                    truth,
                                    reason,
                                    "A catalog or Kubernetes check failed; inspect supervisor logs for the failed operation and cause.",
                                    deployment["metadata"]["generation"],
                                    now,
                                )
                            try:
                                kube.status(deployment, status)
                            except (KubeError, httpx.HTTPError) as status_error:
                                logger.warning("Could not update status: %s", describe_failure(status_error))
            except (KubeError, httpx.HTTPError) as exc:
                logger.warning("Kubernetes API unavailable: %s", describe_failure(exc))
            for _ in range(15):
                if stopped:
                    break
                time.sleep(1)


if __name__ == "__main__":
    main()
