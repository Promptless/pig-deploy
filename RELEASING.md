# Release operations

The source prepares version 0.3.3 with the analyzer's NUL ingestion repair. The stable catalog selects
the published 0.3.1 release. Local tests are not installation acceptance. Versions 0.3.0
and 0.3.1 are AWS-only releases. Report-backed acceptance covers a real AWS clean
installation and canonical pipeline, plus the unattended 0.3.0 to 0.3.1 patch update
for 0.3.1. Version 0.3.1 may instead use the explicit owner sign-off described below.
Azure and GCP support remains experimental; these releases do not
establish cloud validation for their native adapters or deployment modules.

## Artifact and trust boundary

A release binds the exact public source commit, immutable analyzer and supervisor
GHCR image digests, CRD bytes, chart versions/package checksums/OCI digests, and a
checksummed source archive containing Terraform modules and examples. Only
tracked public source is packaged. Chart tar/gzip metadata is normalized so a
retry produces identical bytes. An existing version with different bytes is a
hard error; published versions are never replaced.

The stable catalog is delivered over HTTPS from this repository. Manifest and
CRD URLs must use full commit IDs, and fetched bytes must match their SHA-256.
Repository and protected publication access are the release trust root. Checksums
establish integrity against that catalog; they are not an independent signature
system. Package visibility must permit anonymous access.

## Repository setup

An authorized repository administrator must configure the `pig-release`
environment with required reviewers, protect `main` and release tags, and permit
Actions to open draft pull requests. Give publication workflows only the
repository/package permissions they need. Configure the two runtime image
packages and chart packages for public visibility; first-time GHCR publication
may require a package administrator to change visibility before an anonymous
check can pass. No customer secret belongs in this repository or its evidence.

## Candidate and acceptance

1. Merge reviewed public source and the compatible private analyzer implementation.
   The analyzer's deployment-capabilities command must report controller protocol
   1, schema revision 4, native `s3`/`azureBlob`/`gcs`, and the `preflight`,
   `supervised-migrate`, `verify`, and `acceptance` commands. Its capabilities must
   include `native-storage-v1`, `migration-ledger-v1`, `installation-identity-v1`,
   `alembic-migrations-v1`, and `storage-readiness-v1`.
   The identity capability ensures the analyzer can resolve its installation from
   its credential without an operator-supplied deployment ID.
   Its private image workflow publishes a SHA tag, never a mutable release tag.
2. With explicit publication authorization, run the supervisor candidate workflow
   on `main`. Record both image digests and the exact public source commit. The
   supervisor image embeds that commit as its OCI revision and reports its version.
3. For 0.3.0 and 0.3.1, install the exact candidate into a fresh EKS namespace using S3,
   verified PostgreSQL TLS, native workload identity, and the public supervisor
   path. Prove enrollment, a canonical object written and readable at its exact
   native URI, successful analysis of the matching fingerprint, hosted
   acknowledgement, and the matching trace and analysis in the Dashboard.
   Zero findings is a successful analysis. An image pull, HTTP 200, healthy Pods,
   or green CI does not establish this result.
   For 0.3.1, also complete the [AWS unattended patch-update test](catalog/testing/aws-updates/README.md)
   and prove a fresh trace completes the same pipeline after the upgrade.
4. Add `releases/acceptance/VERSION.json` in a reviewed PR. Its shape is generated
   in [schemas/acceptance.json](schemas/acceptance.json). Supply `version`,
   `sourceCommit`, exact `analyzerImage`/`supervisorImage`, `requirementsDigest`,
   optional `rollbackTo`, and an `eks` record with `testedAt` and an `evidence`
   map containing `install` and `canonicalAcceptance`, plus `patchUpdate` for 0.3.1.

   For 0.3.0 and 0.3.1, `aks` and `gke` reports are optional experimental evidence. Any
   supplied report must cover that version's required checks and meet the same freshness and URL
   rules. Additional lifecycle checks may be recorded when actually tested.
   A nonempty `rollbackTo` requires a `recovery` report for every required cloud.

   The AWS-only exception applies only to 0.3.0 and 0.3.1. Later versions retain
   the EKS, AKS, and GKE gate with all nine checks until a reviewed policy change:

   ```text
   install canonicalAcceptance minorUpdate majorUpdate pausePin
   blockedResume secretRotation supervisorSelfUpdate recovery
   ```

   Evidence values are sanitized public HTTPS report links. Reports must establish
   the claimed behavior for these exact artifacts; do not include customer trace
   content, installation tokens, DSNs, or private infrastructure identifiers.
   A reviewer checks report contents. The workflow validates structure, binding,
   and freshness; it does not independently execute or audit the evidence URLs.

   **Owner sign-off for 0.3.1:** the release owner may explicitly approve AWS-only
   publication and stable promotion in place of stored test reports. Retain all
   exact source, image, and requirements identities, omit the cloud reports, and
   supply `ownerSignOff` with `approvedBy`, timezone-aware `approvedAt`,
   `scope: "aws-only"`, and a `statement` explaining the approval. This is an
   approval record, not a claim that report-backed testing was verified. It must
   not contain `testedAt`, invented report URLs, or advertised rollback paths.
   The exception applies only to 0.3.1; image execution, capabilities, artifact
   integrity, anonymous access, and catalog review remain mandatory.
5. Compute `requirementsDigest` as SHA-256 of the requirements model's canonical
   JSON: parse `releases/requirements/VERSION.json` with `Requirements`, dump with
   aliases, then serialize with sorted keys and compact separators. This internal
   evidence digest is 64 lowercase hex characters without the `sha256:` prefix.
   Test evidence and owner sign-off expire after 14 days. `rollbackTo` contains bare manifest digests
   for rollback paths actually included in recovery acceptance.

### Schema-4 candidate

The 0.3.3 requirements accept starting schema revisions 0–3 and target revision 4.
The worker also accepts the target revision for retries and configuration rotation.
The image must advertise `alembic-migrations-v1` and `storage-readiness-v1`.
Worker CI runs installation and recovery tests on PostgreSQL 15–18 before customer GHCR image
publication. Keep the supervisor's stop, migrate, start sequence.

Alembic adopts known predecessors after checking their history and layout.
Conflicting locations, unknown checksums, and incomplete schemas abort the
transaction. `destructiveMigration: false` means the migration preserves data;
recovery still requires a schema-4-compatible image or forward repair. Include a
`rollbackTo` entry only for a tested path between releases with the same schema.

Exercise [storage recovery](STORAGE.md#recover-from-a-backup) with the target
artifact before certifying a cloud recovery path. Local transaction and object
repair tests do not substitute for that evidence.

Select instruction repositories in PIG Settings through the organization's GitHub
connection. The analyzer retrieves the selected-source catalog and repository
access through Runtime. Deployment configuration carries model and infrastructure
credentials; it does not carry repository identities or a repository token.

Define upgrade fixtures before testing upgrades. Each minor or major transition
needs immutable source and image identities, a compatible supervisor-capable
baseline, and a real change to exercise. Relabelling the same image or using an
incompatible worker does not prove an upgrade.

## Publication and promotion

After evidence is merged, explicitly dispatch **Publish accepted deployment
release** on `main` with `version`. Protected environment approval is the release
operator's final authorization gate.

The workflow selects the accepted commit, checks any existing tag points there,
then anonymously pulls both runtime images. It verifies supervisor source/version
and executes the analyzer capability command before packaging either chart.
Release policy and publication tooling run from the reviewed workflow commit;
charts, requirements, CRDs, and the source archive come from the accepted source
commit. This lets reviewed release-policy changes apply without rebuilding or
relabeling the accepted runtime images.
Only absent chart versions are pushed. Existing versions must match the exact
package bytes, allowing a failed anonymous-access step to be retried after an
administrator fixes package visibility.

Both OCI charts must pull anonymously and match their package checksums. The
workflow then creates the immutable source tag and GitHub release, anonymously
downloads every asset, and compares exact bytes and `SHA256SUMS`. It opens a
**draft catalog promotion PR**; it never merges that PR. A partial existing GitHub
release with missing or mismatched assets fails closed and needs operator repair
or a new version, without overwriting already published bytes.

The promotion uses two commits: immutable manifest first, then a stable-index
entry pointing at that commit. Review the immutable URLs and evidence before
merging. If PR creation is interrupted after the branch push, rerun publication:
it verifies the existing manifest bytes, digest, and reachable immutable commit,
then creates or locates the PR without rewriting the branch. A mismatched branch
or closed, unmerged PR requires operator review.
If organization policy blocks Actions from creating PRs, the operator can open a
draft PR from the published `release/catalog-VERSION` branch to `main`, then rerun
publication. The retry verifies the branch and reuses that PR; organization-wide
Actions permissions do not need to change.
Merging makes the release eligible for automatic updates, including
major versions. Do not squash away or delete the manifest commit referenced by
its URL. Keep releases, tags, and those commits reachable and protected.

## Scope of acceptance

The automated suite covers native chart rendering, CRD admission, scope-limited
RBAC, fake-API restart/retry behavior, recovery/capacity binding, credential
rotation, immutable catalog selection, publication integrity, and mock cloud
plans. The private worker suite covers adapters, persisted native locations,
migration compatibility, and bounded maintenance failures. The credential-free
[Kubernetes CI suite](CI.md) also exercises real Helm install/upgrade, process
handoff, admission, status conflicts, SSA, and RBAC in disposable kind clusters.

For 0.3.0 and report-backed 0.3.1 acceptance, live evidence covers a clean AWS installation, verified
PostgreSQL TLS and S3 access through workload identity, and the full host pipeline
through Dashboard confirmation. Both runtime images and charts must be anonymously
pullable, and publication and stable activation still require review of the
concrete artifacts and evidence.
Report-backed 0.3.1 acceptance additionally covers the real AWS patch upgrade and
fresh pipeline acceptance after that upgrade. An owner sign-off substitutes an
explicit release decision for these stored reports; it does not prove their results.

These AWS release gates do not establish minor/major upgrade behavior,
pause/pin and rotation in a real cloud, interrupted-migration recovery, long-lived
workload identity refresh, supervisor self-update with the real analyzer, or
Azure/GCP installation. Preserve those engineering checks for subsequent release
validation and for promoting experimental cloud support. Define genuine upgrade
and recovery fixtures before running them; a version-label change is not an upgrade.
A Docker build runs in pull-request CI; local validation does not require Docker.
