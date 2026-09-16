"""Resume catalog promotion without rewriting an already published manifest commit."""

import json
import re
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path

from .models import Release


def promote_draft(root: Path, manifest: Path) -> str:
    """Verify/reuse a pushed promotion, then create or locate its draft PR."""
    from .publication import promote_catalog

    payload = manifest.read_bytes()
    release = Release.model_validate_json(payload)
    if release.artifacts is None:
        raise ValueError("published release is missing its artifact inventory")
    version = release.version
    branch = f"release/catalog-{version}"
    manifest_path = f"catalog/releases/{version}.json"

    def run(*args: str) -> str:
        return subprocess.check_output(args, cwd=root, text=True).strip()

    def verify(ref: str) -> None:
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
        commit = url[1]
        run("git", "merge-base", "--is-ancestor", commit, ref)
        for revision in (commit, ref):
            existing = subprocess.check_output(["git", "show", f"{revision}:{manifest_path}"], cwd=root)
            if existing != payload:
                raise ValueError("existing promotion manifest bytes differ from accepted artifacts")

    run("git", "fetch", "origin", "main")
    main_catalog = json.loads(run("git", "show", "origin/main:catalog/stable.json"))
    already_promoted = any(entry["version"] == version for entry in main_catalog["releases"])
    promotion_commit = None
    if already_promoted:
        verify("origin/main")
    else:
        remote = run("git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        if remote:
            run("git", "fetch", "origin", f"refs/heads/{branch}")
            verify("FETCH_HEAD")
            promotion_commit = run("git", "rev-parse", "FETCH_HEAD")
            base = run("git", "merge-base", "origin/main", "FETCH_HEAD")
            changed = set(run("git", "diff", "--name-only", base, "FETCH_HEAD").splitlines())
            catalog = json.loads(run("git", "show", "FETCH_HEAD:catalog/stable.json"))
            catalog["releases"] = [entry for entry in catalog["releases"] if entry["version"] != version]
            if changed != {manifest_path, "catalog/stable.json"} or catalog != json.loads(
                run("git", "show", f"{base}:catalog/stable.json")
            ):
                raise ValueError("existing promotion contains unrelated changes")
        else:
            run("git", "switch", "--detach", "origin/main")
            path = root / manifest_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            run("git", "add", manifest_path)
            run("git", "commit", "-m", f"Add immutable manifest for PIG {version}")
            commit = run("git", "rev-parse", "HEAD")
            promote_catalog(root, path, commit)
            run("git", "add", "catalog/stable.json")
            run("git", "commit", "-m", f"Select PIG {version} in the stable catalog")
            promotion_commit = run("git", "rev-parse", "HEAD")
            # An absent-ref lease also rejects a concurrently created branch, even
            # if our commits happen to descend from it. Never rewrite published refs.
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
            run("git", "merge-base", "--is-ancestor", pull["headRefOid"], "origin/main")
            verify(pull["headRefOid"])
            return pull["url"]
        if pull["state"] == "OPEN" and not already_promoted and pull["headRefOid"] == promotion_commit:
            return pull["url"]
    if pulls or already_promoted:
        raise ValueError("existing promotion has no matching open or merged PR; inspect its review state")
    with tempfile.TemporaryDirectory(prefix="pig-promotion-") as directory:
        body = Path(directory) / "body.md"
        body.write_text(
            f"Promote the accepted PIG {version} release. Both runtime images, chart packages, and source archive "
            "passed anonymous access checks. Three-cloud acceptance evidence is in "
            f"releases/acceptance/{version}.json. Merging this catalog entry makes the release eligible for "
            "automatic updates, including major upgrades.\n"
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
            f"Promote PIG {version} to stable",
            "--body-file",
            str(body),
        )
