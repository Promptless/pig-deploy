"""Exercise promotion retries with real local Git remotes and a stubbed GitHub API."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from pig_supervisor.promotion import promote_draft

BRANCH = "release/catalog-0.3.0"
URL = "https://github.com/Promptless/pig-deploy/pull/123"


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.fixture
def promotion(tmp_path, monkeypatch):
    root, remote = tmp_path / "source", tmp_path / "remote.git"
    root.mkdir()
    git(root, "init", "--bare", str(remote))
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Release test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "remote", "add", "origin", str(remote))
    (root / "catalog").mkdir()
    (root / "catalog/stable.json").write_text('{"schemaVersion":1,"releases":[]}\n')
    git(root, "add", "catalog")
    git(root, "commit", "-m", "Initial catalog")
    git(root, "push", "origin", "main")
    manifest = tmp_path / "release.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "0.3.0",
                "analyzerImage": "ghcr.io/promptless/pig-trace-analyzer@sha256:" + "a" * 64,
                "supervisorImage": "ghcr.io/promptless/pig-supervisor@sha256:" + "b" * 64,
                "requirements": {"storageBackends": ["s3"], "schemaFrom": [0, 1], "schemaTo": 1},
                "artifacts": {
                    "sourceCommit": git(root, "rev-parse", "HEAD"),
                    "terraformArchiveURL": "https://github.com/Promptless/pig-deploy/releases/download/v0.3.0/pig-deploy-0.3.0.tar.gz",
                    "terraformArchiveSha256": "c" * 64,
                    **{
                        key: {
                            "repository": "oci://ghcr.io/promptless/charts/" + name,
                            "version": "0.3.0",
                            "sha256": "d" * 64,
                            "ociDigest": "sha256:" + "e" * 64,
                        }
                        for key, name in [
                            ("supervisorChart", "pig-supervisor"),
                            ("workerChart", "pig-trace-analyzer"),
                        ]
                    },
                },
            }
        )
        + "\n"
    )
    github = SimpleNamespace(fail_create=True, pulls=[], creates=0)
    check_output = subprocess.check_output

    def run(args, **kwargs):
        if args[0] != "gh":
            return check_output(args, **kwargs)
        if args[1:3] == ("pr", "list"):
            return json.dumps(github.pulls)
        assert args[1:3] == ("pr", "create")
        assert "--draft" in args
        github.creates += 1
        if github.fail_create:
            raise subprocess.CalledProcessError(1, args, "PR creation interrupted")
        github.pulls = [
            {
                "url": URL,
                "state": "OPEN",
                "headRepository": {"nameWithOwner": "Promptless/pig-deploy"},
                "headRefOid": git(remote, "rev-parse", "refs/heads/" + BRANCH),
            }
        ]
        return URL + "\n"

    monkeypatch.setattr(subprocess, "check_output", run)
    return root, remote, manifest, github


def test_resume_after_push_reuses_manifest_commit_and_existing_pr(promotion, tmp_path):
    root, remote, manifest, github = promotion
    with pytest.raises(subprocess.CalledProcessError):
        promote_draft(root, manifest)
    tip = git(remote, "rev-parse", "refs/heads/" + BRANCH)
    index = git(remote, "show", f"{tip}:catalog/stable.json")
    clone = tmp_path / "retry"
    git(root, "clone", "--branch", "main", str(remote), str(clone))
    github.fail_create = False
    assert promote_draft(clone, manifest) == URL
    assert promote_draft(clone, manifest) == URL
    assert github.creates == 2  # One interrupted request, then one successful request.
    assert git(remote, "rev-parse", "refs/heads/" + BRANCH) == tip
    assert git(remote, "show", f"{tip}:catalog/stable.json") == index
    manifest_commit = json.loads(index)["releases"][0]["url"].split("/")[5]
    assert git(remote, "merge-base", "--is-ancestor", manifest_commit, tip) == ""


@pytest.mark.parametrize("mismatch", ["bytes", "digest", "commit", "unrelated"])
def test_resume_rejects_modified_promotion_without_rewriting_it(promotion, mismatch):
    root, remote, manifest, github = promotion
    with pytest.raises(subprocess.CalledProcessError):
        promote_draft(root, manifest)
    if mismatch == "bytes":
        (root / "catalog/releases/0.3.0.json").write_text(manifest.read_text() + "\n")
    elif mismatch == "unrelated":
        (root / "unrelated.txt").write_text("unexpected change\n")
    else:
        path = root / "catalog/stable.json"
        catalog = json.loads(path.read_text())
        if mismatch == "digest":
            catalog["releases"][0]["sha256"] = "f" * 64
        else:
            catalog["releases"][0]["url"] = catalog["releases"][0]["url"].replace(
                catalog["releases"][0]["url"].split("/")[5], "main"
            )
        path.write_text(json.dumps(catalog))
    git(root, "add", "catalog", "unrelated.txt") if mismatch == "unrelated" else git(root, "add", "catalog")
    git(root, "commit", "-m", "Change existing promotion")
    git(root, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    tip = git(remote, "rev-parse", "refs/heads/" + BRANCH)
    github.fail_create = False
    with pytest.raises(ValueError):
        promote_draft(root, manifest)
    assert github.creates == 1
    assert git(remote, "rev-parse", "refs/heads/" + BRANCH) == tip


def test_retry_after_merge_and_branch_deletion_returns_merged_pr(promotion):
    root, _remote, manifest, github = promotion
    github.fail_create = False
    assert promote_draft(root, manifest) == URL
    git(root, "push", "origin", "HEAD:refs/heads/main")
    git(root, "push", "origin", "--delete", BRANCH)
    github.pulls[0]["state"] = "MERGED"
    assert promote_draft(root, manifest) == URL
    assert github.creates == 1


def test_closed_unmerged_promotion_requires_review(promotion):
    root, _remote, manifest, github = promotion
    github.fail_create = False
    promote_draft(root, manifest)
    github.pulls[0]["state"] = "CLOSED"
    with pytest.raises(ValueError, match="review state"):
        promote_draft(root, manifest)
    assert github.creates == 1


@pytest.mark.parametrize("foreign_state", ["OPEN", "CLOSED"])
def test_same_named_fork_pr_does_not_replace_canonical_promotion(promotion, foreign_state):
    root, _remote, manifest, github = promotion
    github.fail_create = False
    github.pulls = [
        {
            "url": "https://github.com/Promptless/pig-deploy/pull/122",
            "state": foreign_state,
            "headRepository": {"nameWithOwner": "someone/pig-deploy"},
            "headRefOid": "f" * 40,
        }
    ]
    assert promote_draft(root, manifest) == URL
    assert github.creates == 1


def test_existing_pr_must_match_verified_remote_commit(promotion):
    root, _remote, manifest, github = promotion
    github.fail_create = False
    promote_draft(root, manifest)
    github.pulls[0]["headRefOid"] = "f" * 40
    with pytest.raises(ValueError, match="review state"):
        promote_draft(root, manifest)
    assert github.creates == 1
