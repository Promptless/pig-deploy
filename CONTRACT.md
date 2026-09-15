# Deployment contract

## Ownership

Terraform owns cloud database, storage, network, IAM, sizing, and backup settings.
PIG only probes dependencies and writes application data. Its supervisor has no
cloud credentials or cloud infrastructure API permissions.

Install one supervisor per watched namespace and create exactly one
`PIGDeployment` there. A fixed namespace Lease fences competing controllers;
multiple deployment policies block reconciliation. Separate namespaces can share
the CRD, so automatic CRD changes must be optional, additive, and within the
named CRD permissions granted at bootstrap. New capabilities block the release
until an operator reviews and applies a bootstrap upgrade.

GitOps owns `PIGDeployment`, external Secret delivery, the analyzer ServiceAccount,
and public CA ConfigMaps. PIG owns the generated analyzer Deployment, Service,
Ingress, and maintenance Jobs through Kubernetes owner references. It refuses to
adopt existing resources owned by another party. Do not reconcile those generated
objects with another controller. Suspend a matching Flux bootstrap HelmRelease
before creating `PIGDeployment`. Terraform has no Helm or Kubernetes resources.

## Configuration

Use [examples/pig-deployment.yaml](examples/pig-deployment.yaml) and the generated
[CR schema](schemas/pigdeployment-spec.json). Terraform's `deployment_configuration`
output supplies PostgreSQL metadata, exactly one native storage block,
`service_account_annotations`, and `pod_labels`. It contains no credential values.
Copy only those relevant fields into customer-owned Kubernetes configuration.

Deliver `pig-credentials` with `install-token`, `postgres-dsn`, `model-api-key`,
and, for private repositories, `repository-token`. The PostgreSQL DSN must use
`sslmode=verify-full`. `storage.postgres.caConfigMapRef` mounts its selected key at
`/etc/pig/postgres-ca/ca.pem` and sets `PGSSLROOTCERT`. Analyzer and maintenance Jobs
share the same ServiceAccount, native workload identity, labels, and CA mount.
Secrets, CA, and ServiceAccount revisions trigger revalidation and analyzer
restart, including while release updates are paused. PIG never edits these
customer-owned objects.

S3, Azure Blob, and GCS use their native SDK credential chains, including workload
identity token refresh. Storage federation does not configure model access.
The model contract is OpenAI, Azure OpenAI, or Bedrock Mantle, with the supported
provider URL and API key or Bedrock SigV4 authentication. Terraform examples do
not grant model permissions or provision model endpoints.

## Automatic updates

`spec.release.channel: stable` selects the highest stable semantic version,
including major versions. `pinnedVersion` selects an exact published version;
`paused` halts release transitions at a safe checkpoint. An active migration is
allowed to finish before pause takes effect. A pause before the first install
blocks installation.

The supervisor records progress in CR status through preflight, quiesce,
migration, analyzer rollout, verification, and supervisor rollout. It scales the
existing analyzer to zero before migration and starts a single analyzer replica
afterwards. Jobs use immutable images and names bound to the release,
configuration, and saved transition ID. A new transition runs fresh checks rather
than reusing an earlier successful Job. PostgreSQL records schema checksums and release checkpoints in
`pig_schema_migrations` and `pig_deployment_history`. A restart resumes the saved
transition; a failed Job retries after the operator corrects its cause.

Live checks include PostgreSQL version, schema compatibility, verified TLS,
transactional write access, and native object write/read access. Workload
availability is checked before `Ready=True`. A failed dependency check sets
`Blocked=True` with the Job to inspect. Change cloud resources through Terraform;
checks retry automatically. Declared Terraform capacity is never mirrored into
PIG or presented as observed free capacity.

Rollback is available only when the current manifest explicitly lists the target
release digest in `rollbackTo` and both releases use the same schema revision.
If an incomplete update has reached migration, its target manifest controls this
check, even when the old release is still recorded as installed.
Otherwise choose a compatible forward repair release. A running migration cannot
be replaced by a repair release until it terminates. Database or object recovery
is an operator action, never an automatic destructive restore.

## Release-specific confirmation

Set `spec.release.confirmation.configMapRef` to a customer-owned ConfigMap in the
watched namespace. On a pinned release checkout, inspect the verified catalog:

```sh
uv run --frozen pig-release-inspect --version VERSION
```

`releaseDigest` binds the exact manifest bytes; `capacityRequirementsDigest`
binds the ordered `operatorCapacityRequirements` list as compact, sorted-key
UTF-8 JSON. Both command outputs use `sha256:` followed by 64 lowercase hex
characters. Never substitute an image digest or invent these values.

Every destructive migration requires recovery confirmation. Its ConfigMap data
must contain `releaseDigest`, `deploymentID` matching `spec.hosted.deploymentID`,
`confirmedAt` with a timezone, `postgresRecoveryPoint`, and `objectRecoveryPoint`.
The timestamp must fall within the release's `recoveryMaxAgeHours`. The
supervisor checks it before starting a transition and immediately before starting
a new migration Job, including after a pause.

For capacity prerequisites that PIG cannot directly verify, the same ConfigMap
also needs `capacityConfirmedAt`, `capacityRequirementsDigest`, and
`capacityEvidence`. Review the full requirements and cloud metrics, make any
Terraform changes, then acknowledge that exact release. This records operator
acknowledgement, not a live free-capacity measurement. Recovery confirmation and
capacity acknowledgement are separate checks; neither substitutes for the other.
PIG resumes when live checks and required confirmations pass. Configuration-only
rotation does not repeat an already completed release migration.

## Acceptance

`Ready=True` means installed workloads and dependency checks passed. The separate
`Acceptance` condition requires evidence from the real host pipeline: enrolled
host, canonical object recorded as written and readable at its exact native URI,
succeeded analysis of the same canonical revision, and hosted projection
acknowledgement. Zero findings is a valid succeeded analysis. Pods and HTTP 200
responses cannot establish this condition.

A recorded acceptance remains evidence for that release and configuration;
`lastAcceptanceAt` states when it was verified. Periodic checks do not claim that
every later trace has succeeded. Confirm the matching trace and analysis in the
Dashboard during installation acceptance.

## Manual chart

The manual chart is for operator-managed releases. Its pre-upgrade migration
hook requires the operator to quiesce the existing analyzer before upgrading.
It does not enforce the supervisor's release confirmation ConfigMap. Supply a
compatible immutable analyzer image, a verified recovery point, and the shared
analyzer/migration ServiceAccount. Do not install the manual analyzer and a
supervised analyzer for the same deployment.
