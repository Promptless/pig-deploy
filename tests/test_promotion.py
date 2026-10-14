"""Exercise promotion retries with real local Git remotes and a stubbed GitHub API."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from pig_supervisor.promotion import promote_draft

MANIFEST_BRANCH = "release/manifest-0.3.0"
CATALOG_BRANCH = "release/catalog-0.3.0"
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
            branch = args[args.index("--head") + 1]
            return json.dumps([pull for pull in github.pulls if pull["headRefName"] == branch])
        assert args[1:3] == ("pr", "create")
        assert "--draft" in args
        github.creates += 1
        if github.fail_create:
            raise subprocess.CalledProcessError(1, args, "PR creation interrupted")
        branch = args[args.index("--head") + 1]
        github.pulls.append(
            {
                "url": URL,
                "state": "OPEN",
                "headRepository": {"nameWithOwner": "Promptless/pig-deploy"},
                "headRefOid": git(remote, "rev-parse", "refs/heads/" + branch),
                "headRefName": branch,
            }
        )
        return URL + "\n"

    monkeypatch.setattr(subprocess, "check_output", run)
    return root, remote, manifest, github


def squash_merge(root, github, branch):
    git(root, "fetch", "origin", "main")
    git(root, "fetch", "origin", branch)
    tip = git(root, "rev-parse", "FETCH_HEAD")
    git(root, "switch", "--detach", "origin/main")
    git(root, "merge", "--squash", tip)
    git(root, "commit", "-m", "Merge reviewed " + branch)
    commit = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "HEAD:refs/heads/main")
    git(root, "push", "origin", "--delete", branch)
    for pull in github.pulls:
        if pull["headRefName"] == branch:
            pull["state"] = "MERGED"
    return commit


def merge_manifest(promotion):
    root, _remote, manifest, github = promotion
    github.fail_create = False
    assert promote_draft(root, manifest) == URL
    return squash_merge(root, github, MANIFEST_BRANCH)


@pytest.mark.parametrize("catalog_phase", [False, True])
def test_resume_after_push_reuses_existing_branch_and_pr(promotion, tmp_path, catalog_phase):
    root, remote, manifest, github = promotion
    if catalog_phase:
        merge_manifest(promotion)
    branch = CATALOG_BRANCH if catalog_phase else MANIFEST_BRANCH
    github.fail_create = True
    with pytest.raises(subprocess.CalledProcessError):
        promote_draft(root, manifest)
    tip = git(remote, "rev-parse", "refs/heads/" + branch)
    creates = github.creates
    clone = tmp_path / "retry"
    git(root, "clone", "--branch", "main", str(remote), str(clone))
    github.fail_create = False
    assert promote_draft(clone, manifest) == URL
    assert promote_draft(clone, manifest) == URL
    assert github.creates == creates + 1
    assert git(remote, "rev-parse", "refs/heads/" + branch) == tip


@pytest.mark.parametrize("catalog_phase", [False, True])
@pytest.mark.parametrize("mismatch", ["bytes", "unrelated"])
def test_resume_rejects_modified_promotion_without_rewriting_it(promotion, catalog_phase, mismatch):
    root, remote, manifest, github = promotion
    if catalog_phase:
        merge_manifest(promotion)
    branch = CATALOG_BRANCH if catalog_phase else MANIFEST_BRANCH
    github.fail_create = True
    with pytest.raises(subprocess.CalledProcessError):
        promote_draft(root, manifest)
    if mismatch == "bytes":
        (root / "catalog/releases/0.3.0.json").write_text(manifest.read_text() + "\n")
    else:
        (root / "unrelated.txt").write_text("unexpected change\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Change existing promotion")
    git(root, "push", "origin", f"HEAD:refs/heads/{branch}")
    tip = git(remote, "rev-parse", "refs/heads/" + branch)
    creates = github.creates
    github.fail_create = False
    with pytest.raises(ValueError):
        promote_draft(root, manifest)
    assert github.creates == creates
    assert git(remote, "rev-parse", "refs/heads/" + branch) == tip


@pytest.mark.parametrize("mismatch", ["digest", "commit", "other-release"])
def test_catalog_retry_rejects_invalid_selection(promotion, mismatch):
    root, _remote, manifest, github = promotion
    merge_manifest(promotion)
    github.fail_create = True
    with pytest.raises(subprocess.CalledProcessError):
        promote_draft(root, manifest)
    path = root / "catalog/stable.json"
    catalog = json.loads(path.read_text())
    entry = catalog["releases"][0]
    if mismatch == "digest":
        entry["sha256"] = "f" * 64
    elif mismatch == "commit":
        entry["url"] = entry["url"].replace(entry["url"].split("/")[5], "main")
    else:
        catalog["releases"].append({**entry, "version": "0.3.1"})
    path.write_text(json.dumps(catalog))
    git(root, "add", "catalog")
    git(root, "commit", "-m", "Change existing selection")
    git(root, "push", "origin", f"HEAD:refs/heads/{CATALOG_BRANCH}")
    with pytest.raises(ValueError):
        promote_draft(root, manifest)


def test_squash_merges_retain_manifest_before_selecting_stable(promotion):
    root, remote, manifest, github = promotion
    manifest_commit = merge_manifest(promotion)
    assert json.loads(git(remote, "show", "main:catalog/stable.json"))["releases"] == []
    assert promote_draft(root, manifest) == URL
    catalog = json.loads(git(remote, "show", f"{CATALOG_BRANCH}:catalog/stable.json"))
    assert catalog["releases"][0]["url"].split("/")[5] == manifest_commit
    squash_merge(root, github, CATALOG_BRANCH)
    assert promote_draft(root, manifest) == URL
    assert git(remote, "merge-base", "--is-ancestor", manifest_commit, "main") == ""
    assert git(remote, "show", f"{manifest_commit}:catalog/releases/0.3.0.json") == manifest.read_text().strip()
    assert github.creates == 2


def test_merged_manifest_cannot_be_replaced(promotion):
    root, _remote, manifest, _github = promotion
    merge_manifest(promotion)
    manifest.write_text(manifest.read_text() + "\n")
    with pytest.raises(ValueError, match="manifest bytes"):
        promote_draft(root, manifest)


@pytest.mark.parametrize("catalog_phase", [False, True])
def test_closed_unmerged_promotion_requires_review(promotion, catalog_phase):
    root, _remote, manifest, github = promotion
    if catalog_phase:
        merge_manifest(promotion)
    github.fail_create = False
    promote_draft(root, manifest)
    github.pulls[-1]["state"] = "CLOSED"
    with pytest.raises(ValueError, match="review state"):
        promote_draft(root, manifest)


def test_same_named_fork_pr_does_not_replace_canonical_promotion(promotion):
    root, _remote, manifest, github = promotion
    github.fail_create = False
    github.pulls = [
        {
            "url": "https://github.com/Promptless/pig-deploy/pull/122",
            "state": "CLOSED",
            "headRepository": {"nameWithOwner": "someone/pig-deploy"},
            "headRefOid": "f" * 40,
            "headRefName": MANIFEST_BRANCH,
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
