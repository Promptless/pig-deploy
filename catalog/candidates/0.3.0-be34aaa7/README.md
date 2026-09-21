# AWS installation candidate 0.3.0

This candidate uses public source `be34aaa7cecfa0bd5c424c8866589bab92f9558e`
and targets analyzer schema 3. It is for the authorized AWS acceptance installation;
it is not a published stable release or completed cloud acceptance. The historical
`0.3.0-fcfe56ab` candidate is retained for its immutable references and must not be
used with the current analyzer.

`inventory.json` records exact source commits, runtime image digests, requirements
digest, candidate workflow, and image verification. `release.json` binds those
images to the reviewed requirements and checksummed CRD from the public source.
Published chart/archive metadata is absent because acceptance and publication
remain pending. `rollbackTo` is empty; no recovery path is claimed as tested.

Bootstrap `charts/pig-supervisor` from the recorded public source with the recorded
supervisor image digest. Set `releaseCatalogURL` to the immutable URL of this
directory's `catalog.json` and `watchNamespace` to the prepared test namespace.
Pin the PIGDeployment to `0.3.0`. Supply the customer-owned credential Secret,
PostgreSQL CA, ServiceAccount, endpoint, storage, and model configuration. Select
repositories in PIG Settings; the deployment needs no repository identity or token.
The analyzer resolves its installation from its credential.

The index references the manifest's full commit and checksum. Preserve both commits
while the environment or its reports use them: merge this branch with a merge
commit, and retain its history. The supervisor renders candidate workloads without
requiring final chart packages. Stable remains unchanged and canonical acceptance
is explicitly incomplete.

After the real host pipeline passes, submit reviewed evidence for these exact
artifacts through the normal publication process. Published charts and the final
stable catalog still require a fresh customer-style installation and canonical
acceptance check.
