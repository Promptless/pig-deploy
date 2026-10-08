"""The offline handoff preserves network settings and refuses ambiguous ownership."""

import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "handoff", Path(__file__).resolve().parents[1] / "scripts/ingress-handoff.py"
)
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)


@pytest.fixture
def documents():
    deployment = {"kind": "PIGDeployment", "metadata": {"name": "acme", "namespace": "pig", "uid": "owner-uid"}}
    ingress = {
        "kind": "Ingress",
        "metadata": {
            "name": "acme-analyzer",
            "namespace": "pig",
            "uid": "ingress-uid",
            "resourceVersion": "10",
            "ownerReferences": [
                {
                    "apiVersion": "governance.promptless.ai/v1alpha1",
                    "kind": "PIGDeployment",
                    "name": "acme",
                    "uid": "owner-uid",
                    "controller": True,
                }
            ],
            "labels": {"governance.promptless.ai/deployment": "acme"},
            "annotations": {"alb.ingress.kubernetes.io/certificate-arn": "existing-certificate"},
        },
        "spec": {"ingressClassName": "alb", "rules": [{"host": "pig.example.com"}]},
    }
    return ingress, deployment


def test_handoff_guards_current_object_and_preserves_metadata(documents):
    ingress, deployment = documents
    original = copy.deepcopy(ingress)
    patch = handoff.handoff_patch(ingress, deployment, "pig-supervisor", "pig-system")
    assert patch[:3] == [
        {"op": "test", "path": "/metadata/uid", "value": "ingress-uid"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "10"},
        {"op": "test", "path": "/metadata/ownerReferences", "value": ingress["metadata"]["ownerReferences"]},
    ]
    assert all(operation["path"].startswith("/metadata/") for operation in patch)
    labels = next(operation["value"] for operation in patch if operation["path"] == "/metadata/labels")
    annotations = next(operation["value"] for operation in patch if operation["path"] == "/metadata/annotations")
    assert labels == {**ingress["metadata"]["labels"], "app.kubernetes.io/managed-by": "Helm"}
    assert annotations == {
        **ingress["metadata"]["annotations"],
        "meta.helm.sh/release-name": "pig-supervisor",
        "meta.helm.sh/release-namespace": "pig-system",
    }
    assert ingress == original


@pytest.mark.parametrize("conflict", ["uid", "multiple", "unowned", "helm", "manager", "name", "namespace", "deleting"])
def test_handoff_refuses_foreign_or_ambiguous_ownership(documents, conflict):
    ingress, deployment = documents
    meta = ingress["metadata"]
    if conflict == "deleting":
        meta["deletionTimestamp"] = "2026-10-08T00:00:00Z"
    elif conflict == "uid":
        meta["ownerReferences"][0]["uid"] = "another-deployment"
    elif conflict == "multiple":
        meta["ownerReferences"].append(dict(meta["ownerReferences"][0]))
    elif conflict == "unowned":
        meta.pop("ownerReferences")
    elif conflict == "helm":
        meta["annotations"]["meta.helm.sh/release-name"] = "another-release"
    elif conflict == "manager":
        meta["labels"]["app.kubernetes.io/managed-by"] = "another-manager"
    else:
        meta[conflict] = "different"
    with pytest.raises(ValueError):
        handoff.handoff_patch(ingress, deployment, "pig-supervisor", "pig-system")
