# AWS unattended update test

This catalog exercises automatic updates in the dedicated AWS acceptance
installation. It starts with the accepted schema-3 candidate for version 0.3.0.
Its entries reference immutable manifests; the index can advance independently.
The public stable catalog and its publication gates are unchanged.

## Prepare the installation

Use the supervisor chart's `releaseCatalogURL` setting:

```text
https://raw.githubusercontent.com/Promptless/pig-deploy/main/catalog/testing/aws-updates/catalog.json
```

The installed 0.3.0 manifest must match this catalog's original entry. Set
`spec.release.channel: stable`, `paused: false`, and clear `pinnedVersion`.
Wait for the existing release to be ready before recording the baseline. These
are test setup changes; record subsequent customer configuration changes as
interventions.

## Prepare the successor

Build the reviewed 0.3.1 analyzer and supervisor sources. Verify anonymous pulls,
source identities, package versions, and analyzer capabilities against
`releases/requirements/0.3.1.json`. The analyzer includes actual dependency
security updates relative to the installed candidate and keeps schema revision 3.

Commit the new manifest with the exact image digests, requirements, and CRD hash.
Retain its commit so its immutable URL stays reachable. Add the manifest to this
index in a separate commit, preserving the original 0.3.0 entry unchanged. An
existing version must never acquire different manifest bytes.

## Observe the update

After advancing the catalog, let the supervisor reconcile without Helm upgrades,
workload image patches, credential edits, manual migrations, or enrollment resets.
Record phase transitions, Job outcomes, image digests, Pod termination/start times,
and any downtime. Verify that old Pods exit before migration begins, the migration
ledger stays intact, and existing trace fingerprints and object references survive.

Require the new release to reach readiness and acceptance. Submit a fresh trace
through the same enrolled host, verify successful analysis of its current source
fingerprint and hosted acknowledgement, and confirm it in the Dashboard. Existing
acceptance evidence alone does not prove the updated analyzer processed new data.

This test covers a schema-compatible patch update. Supervisor handoff can be
observed, but controller behavior changes, schema-changing migrations, minor and
major updates, failure recovery, and Azure/GCP acceptance need separate fixtures
and evidence. Do not add rollback declarations before testing that recovery path.
