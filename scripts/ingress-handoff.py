"""Print a guarded JSON Patch for handing a PIG-owned Ingress to Helm.

Reads saved Kubernetes JSON documents; never contacts or modifies a cluster.
"""

import argparse
import json
from pathlib import Path


def handoff_patch(ingress: dict, deployment: dict, release: str, release_namespace: str) -> list[dict]:
    meta = ingress["metadata"]
    owner = deployment["metadata"]
    if ingress.get("kind") != "Ingress" or deployment.get("kind") != "PIGDeployment":
        raise ValueError("Expected Ingress and PIGDeployment documents")
    if meta["namespace"] != owner["namespace"] or meta["name"] != owner["name"] + "-analyzer":
        raise ValueError("Ingress name and namespace must match the analyzer Service")
    if meta.get("deletionTimestamp") or owner.get("deletionTimestamp"):
        raise ValueError("Cannot hand off an Ingress or PIGDeployment being deleted")
    expected = {
        "apiVersion": "governance.promptless.ai/v1alpha1",
        "kind": "PIGDeployment",
        "name": owner["name"],
        "uid": owner["uid"],
        "controller": True,
    }
    owners = meta.get("ownerReferences", [])
    if len(owners) != 1 or any(owners[0].get(key) != value for key, value in expected.items()):
        raise ValueError("Ingress must be owned solely by the specified PIGDeployment UID")
    labels = dict(meta.get("labels") or {})
    annotations = dict(meta.get("annotations") or {})
    for key, value in {
        "meta.helm.sh/release-name": release,
        "meta.helm.sh/release-namespace": release_namespace,
    }.items():
        if key in annotations and annotations[key] != value:
            raise ValueError("Ingress already belongs to a different Helm release")
        annotations[key] = value
    if labels.get("app.kubernetes.io/managed-by", "Helm") != "Helm":
        raise ValueError("Ingress already has a different manager")
    labels["app.kubernetes.io/managed-by"] = "Helm"
    return [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "test", "path": "/metadata/ownerReferences", "value": owners},
        {"op": "remove", "path": "/metadata/ownerReferences"},
        {"op": "add", "path": "/metadata/labels", "value": labels},
        {"op": "add", "path": "/metadata/annotations", "value": annotations},
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ingress", required=True, type=Path)
    parser.add_argument("--deployment", required=True, type=Path)
    parser.add_argument("--release-name", required=True)
    parser.add_argument("--release-namespace", required=True)
    args = parser.parse_args()
    try:
        patch = handoff_patch(
            json.loads(args.ingress.read_text()),
            json.loads(args.deployment.read_text()),
            args.release_name,
            args.release_namespace,
        )
    except (KeyError, ValueError) as exc:
        parser.exit(1, f"Cannot prepare ingress handoff: {exc}\n")
    print(json.dumps(patch, indent=2))
