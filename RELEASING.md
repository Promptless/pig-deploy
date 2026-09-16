# Release operations

The first source candidate is 0.3.0. The stable catalog is empty until an accepted
release is published and its catalog promotion is reviewed. Local tests are not
installation acceptance. No three-cloud acceptance record is supplied by this
change, so its publication workflow cannot release an untested candidate.

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
   1, schema revision 1, native `s3`/`azureBlob`/`gcs`, and all maintenance commands.
   Its private image workflow publishes a SHA tag, never a mutable release tag.
2. With explicit publication authorization, run the supervisor candidate workflow
   on `main`. Record both image digests and the exact public source commit. The
   supervisor image embeds that commit as its OCI revision and reports its version.
3. In separately authorized existing EKS, AKS, and GKE test environments, exercise
   installation and real canonical acceptance; minor and major upgrades; pause
   and pin; blocked release and automatic resume; Secret rotation; supervisor
   self-update; and recovery. Test native workload identity refresh and exact
   database TLS/CA behavior. Use approved recovery points before destructive work.
4. Add `releases/acceptance/VERSION.json` in a reviewed PR. Its shape is generated
   in [schemas/acceptance.json](schemas/acceptance.json). Supply `version`,
   `sourceCommit`, exact `analyzerImage`/`supervisorImage`, `requirementsDigest`,
   optional `rollbackTo`, and `eks`, `aks`, `gke` records. Each cloud record needs
   `testedAt` and an `evidence` map with these exact keys:

   ```text
   install canonicalAcceptance minorUpdate majorUpdate pausePin
   blockedResume secretRotation supervisorSelfUpdate recovery
   ```

   Evidence values are sanitized public HTTPS report links. Reports must establish
   the claimed behavior for these exact artifacts; do not include customer trace
   content, installation tokens, DSNs, or private infrastructure identifiers.
   A reviewer checks report contents. The workflow validates structure, binding,
   and freshness; it does not independently execute or audit the evidence URLs.
5. Compute `requirementsDigest` as SHA-256 of the requirements model's canonical
   JSON: parse `releases/requirements/VERSION.json` with `Requirements`, dump with
   aliases, then serialize with sorted keys and compact separators. This internal
   evidence digest is 64 lowercase hex characters without the `sha256:` prefix.
   Acceptance expires after 14 days. `rollbackTo` contains bare manifest digests
   for rollback paths actually included in recovery acceptance.

## Publication and promotion

After evidence is merged, explicitly dispatch **Publish accepted deployment
release** on `main` with `version`. Protected environment approval is the release
operator's final authorization gate.

The workflow selects the accepted commit, checks any existing tag points there,
then anonymously pulls both runtime images. It verifies supervisor source/version
and executes the analyzer capability command before packaging either chart.
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
Merging makes the release eligible for automatic updates, including
major versions. Do not squash away or delete the manifest commit referenced by
its URL. Keep releases, tags, and those commits reachable and protected.

## Release-required engineering acceptance

The automated suite covers native chart rendering, CRD admission, scope-limited
RBAC, fake-API restart/retry behavior, recovery/capacity binding, credential
rotation, immutable catalog selection, publication integrity, and mock cloud
plans. The private worker suite covers adapters, persisted native locations,
migration compatibility, and bounded maintenance failures.

Remaining release gates are real three-cloud installation/update/recovery
acceptance, cloud SDK token refresh under real federation, real controller leader
handoff and Kubernetes admission/SSA behavior, PostgreSQL certificate/network
validation, both runtime images and charts anonymously pullable, and full host
pipeline/Dashboard evidence. A Docker build runs in pull-request CI; local
validation does not depend on a working Docker daemon.
