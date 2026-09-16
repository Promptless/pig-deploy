"""Print verified release requirements and exact customer confirmation digests."""

import argparse
import json

import httpx

from .catalog import canonical_digest, select_release

CATALOG = "https://raw.githubusercontent.com/Promptless/pig-deploy/main/catalog/stable.json"


def main() -> None:
    """Read the public catalog without credentials or Kubernetes/cloud mutations."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="Exact published stable version")
    args = parser.parse_args()
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        selected = select_release(client, CATALOG, args.version)
    requirements = selected.release.requirements
    print(
        json.dumps(
            {
                "version": selected.release.version,
                "releaseDigest": "sha256:" + selected.digest,
                "capacityRequirementsDigest": "sha256:" + canonical_digest(requirements.operator_capacity_requirements),
                "requirements": requirements.model_dump(by_alias=True),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
