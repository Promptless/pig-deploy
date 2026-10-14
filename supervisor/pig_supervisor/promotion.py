"""Promote release manifests through reviewed commits retained on main."""

import json
import re
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path

from .models import Release


def promote_draft(root: Path, manifest: Path) -> str:
    """Open the manifest PR, then its stable-selection PR after the manifest merges."""
    from .publication import promote_catalog

    payload = manifest.read_bytes()
    release = Release.model_validate_json(payload)
    if release.artifacts is None:
        raise ValueError("published release is missing its artifact inventory")
    version = release.version
    manifest_path = f"catalog/releases/{version}.json"

    def run(*args: str) -> str:
        return subprocess.check_output(args, cwd=root, text=True).strip()

    def read_manifest(ref: str) -> bytes:
        return subprocess.check_output(["git", "show", f"{ref}:{manifest_path}"], cwd=root)

    def verify_manifest(ref: str) -> None:
        if read_manifest(ref) != payload:
            raise ValueError("existing promotion manifest bytes differ from accepted artifacts")

    def verify_catalog(ref: str) -> None:
        catalog = json.loads(run("git", "show", f"{ref}:catalog/stable.json"))
        entries = [entry for entry in catalog["releases"] if entry["version"] == version]
        if len(entries) != 1 or entries[0]["sha256"] != sha256(payload).hexdigest():
            raise ValueError("existing promotion does not match the accepted manifest")
        url = re.fullmatch(
            r"https://raw\.githubusercontent\.com/Promptless/pig-deploy/([a-f0-9]{40})/" + re.escape(manifest_path),
            entries[0]["url"],
        )
        if url is None:
            raise ValueError("existing promotion must reference an immutable manifest commit")
        run("git", "merge-base", "--is-ancestor", url[1], "origin/main")
        verify_manifest(url[1])
        verify_manifest(ref)

    run("git", "fetch", "origin", "main")
    main_catalog = json.loads(run("git", "show", "origin/main:catalog/stable.json"))
    already_promoted = any(entry["version"] == version for entry in main_catalog["releases"])
    manifest_merged = bool(run("git", "ls-tree", "--name-only", "origin/main", "--", manifest_path))
    if manifest_merged:
        verify_manifest("origin/main")
    catalog_phase = manifest_merged
    branch = f"release/{'catalog' if catalog_phase else 'manifest'}-{version}"
    changed_path = "catalog/stable.json" if catalog_phase else manifest_path
    promotion_commit = None
    if already_promoted:
        verify_catalog("origin/main")
    else:
        remote = run("git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        if remote:
            run("git", "fetch", "origin", f"refs/heads/{branch}")
            promotion_commit = run("git", "rev-parse", "FETCH_HEAD")
            base = run("git", "merge-base", "origin/main", "FETCH_HEAD")
            changed = set(run("git", "diff", "--name-only", base, "FETCH_HEAD").splitlines())
            if changed != {changed_path}:
                raise ValueError("existing promotion contains unrelated changes")
            verify_manifest("FETCH_HEAD")
            if catalog_phase:
                verify_catalog("FETCH_HEAD")
                catalog = json.loads(run("git", "show", "FETCH_HEAD:catalog/stable.json"))
                catalog["releases"] = [entry for entry in catalog["releases"] if entry["version"] != version]
                if catalog != json.loads(run("git", "show", f"{base}:catalog/stable.json")):
                    raise ValueError("existing promotion contains unrelated catalog changes")
        else:
            run("git", "switch", "--detach", "origin/main")
            if catalog_phase:
                # This commit is already on main, including after a squash merge.
                commit = run("git", "log", "-1", "--format=%H", "origin/main", "--", manifest_path)
                promote_catalog(root, root / manifest_path, commit)
            else:
                path = root / manifest_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            run("git", "add", changed_path)
            message = (
                f"Select PIG {version} in the stable catalog"
                if catalog_phase
                else f"Add immutable manifest for PIG {version}"
            )
            run("git", "commit", "-m", message)
            promotion_commit = run("git", "rev-parse", "HEAD")
            # An absent-ref lease rejects concurrent creation without rewriting it.
            run("git", "push", f"--force-with-lease=refs/heads/{branch}:", "origin", f"HEAD:refs/heads/{branch}")

    pulls = json.loads(
        run(
            "gh",
            "pr",
            "list",
            "--repo",
            "Promptless/pig-deploy",
            "--state",
            "all",
            "--base",
            "main",
            "--head",
            branch,
            "--json",
            "url,state,headRepository,headRefOid",
        )
    )
    pulls = [
        pull for pull in pulls if (pull.get("headRepository") or {}).get("nameWithOwner") == "Promptless/pig-deploy"
    ]
    for pull in pulls:
        if pull["state"] == "MERGED" and already_promoted:
            return pull["url"]
        if pull["state"] == "OPEN" and not already_promoted and pull["headRefOid"] == promotion_commit:
            return pull["url"]
    if pulls or already_promoted:
        raise ValueError("existing promotion has no matching open or merged PR; inspect its review state")
    with tempfile.TemporaryDirectory(prefix="pig-promotion-") as directory:
        body = Path(directory) / "body.md"
        if catalog_phase:
            title = f"Promote PIG {version} to stable"
            description = f"Select PIG {version} using the immutable manifest already merged into main. Merging this catalog entry makes the release eligible for automatic updates, including major upgrades."
        else:
            title = f"Retain the immutable manifest for PIG {version}"
            description = f"Retain the accepted PIG {version} manifest on main. Both runtime images, chart packages, and source archive passed anonymous access checks. After this PR merges, rerun publication to create the stable catalog selection PR."
        body.write_text(
            f"{description}\n\nRelease-specific acceptance or owner sign-off is in releases/acceptance/{version}.json.\n"
        )
        return run(
            "gh",
            "pr",
            "create",
            "--repo",
            "Promptless/pig-deploy",
            "--draft",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            str(body),
        )
