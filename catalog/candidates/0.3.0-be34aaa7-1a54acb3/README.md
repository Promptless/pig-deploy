# AWS installation candidate 0.3.0

This candidate binds public deployment source `be34aaa7cecfa0bd5c424c8866589bab92f9558e`
to analyzer source `1a54acb3abe0dc3914ea065cb679b040d640ed9e`. It supports automatic
default host-enrollment policies and analyzer schema 3. It is for AWS installation
testing; stable publication and canonical acceptance remain incomplete.

`inventory.json` records immutable image digests, source commits, the requirements
digest, build workflows, anonymous manifest checks, and successful execution of both
images in EKS without image-pull Secrets. `release.json` binds those images to the
reviewed requirements and source CRD. Published charts and source archive metadata
are absent. `rollbackTo` is empty because no recovery path has been accepted.

Bootstrap `charts/pig-supervisor` from the recorded public source with the recorded
supervisor image digest. Use the immutable URL of this directory's `catalog.json`
as `releaseCatalogURL`, watch the prepared test namespace, and pin the PIGDeployment
to `0.3.0`. Supply the customer-owned credential Secret, PostgreSQL CA, ServiceAccount,
endpoint, storage, and model configuration. Select repositories in PIG Settings.

Use a fresh installation for this candidate. An in-progress installation using a
different 0.3.0 manifest cannot switch to this catalog: the supervisor enforces the
original version-to-digest binding. Existing candidate directories and their
immutable URLs remain intact. Reinstallation must preserve externally owned
credentials and infrastructure and account for any Ingress/ALB replacement.

The index pins the manifest's full commit and checksum. Merge this PR with a merge
commit and preserve both commits while installations or reports reference them.
After enrollment, canonical storage, analysis, and Dashboard acknowledgement pass,
submit acceptance evidence for these exact artifacts. Final publication and stable
activation require their separate review and customer installation verification.
