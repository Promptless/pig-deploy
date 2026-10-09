# Deployment contract

## Release support

The 0.3.0 release requires a verified clean installation on AWS/EKS and canonical
pipeline acceptance through the Dashboard. Versions 0.3.1, 0.3.3, 0.3.4, and 0.3.5 permit
explicit AWS-only owner sign-off in place of stored test reports. Each release
requires its own approval bound to exact artifacts. Azure/AKS and GCP/GKE are
experimental.
The lifecycle behavior below is the implementation contract; owner sign-off does
not establish report-backed validation of upgrades, controller self-update, credential
rotation, interrupted-migration recovery, or long-lived identity refresh. See
[release acceptance](RELEASING.md) for the evidence required by each release.

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
For changes to required spec fields, follow the
[operator CRD upgrade procedure](UPGRADING.md).

A contender waits one full Lease duration (five minutes) after first observing
an existing Lease or observing its latest renewal before taking over. A newly
started contender begins that wait afresh; another node's wall clock is not
evidence of Lease expiry.

Name the `PIGDeployment` with at most 54 lowercase letters, digits, or hyphens.
Start with a letter and end with a letter or digit so its generated Service name
meets Kubernetes naming rules.

GitOps owns `PIGDeployment`, external Secret delivery, the analyzer ServiceAccount,
and public CA ConfigMaps. Helm/GitOps owns the collector Ingress. PIG owns the
generated analyzer Deployment, Service, and maintenance Jobs through Kubernetes
owner references. It refuses to adopt existing resources owned by another party. Do not reconcile those generated
objects with another controller. Suspend a matching Flux bootstrap HelmRelease
before creating `PIGDeployment`. Terraform has no Helm or Kubernetes resources.

## Configuration

Use [examples/pig-deployment.yaml](examples/pig-deployment.yaml) and the generated
[CR schema](schemas/pigdeployment-spec.json). Terraform's `deployment_configuration`
output supplies PostgreSQL metadata, exactly one native storage block,
`service_account_annotations`, and `pod_labels`. It contains no credential values.
Copy only those relevant fields into customer-owned Kubernetes configuration.

Create a named analyzer installation in Promptless Settings and store its credential
in the Secret referenced by `hosted.installTokenSecretRef`. The analyzer resolves
its installation identity from that credential before serving or running maintenance
commands. Replicas, restarts, and credential replacement use the same installation.
`hosted.runtimeURL` defaults to `https://api.gopromptless.ai`; override it only for
a different Promptless environment.

Credential rotation in Settings immediately revokes the previous credential.
Update the customer-managed Secret with the replacement; the supervisor revalidates
and restarts the analyzer. Hosted access is interrupted until that completes.
Revoking a credential preserves the installation and its history. Issue a replacement
for that installation to restore access. PIG does not write customer Secrets.

Select instruction repositories in Promptless Settings. The analyzer fetches their
identities and access credentials from the hosted runtime.

Deliver `pig-credentials` with `install-token` and `postgres-dsn`. Add
`model-api-key` when the model uses API-key authentication. The PostgreSQL DSN must use
`sslmode=verify-full`. `storage.postgres.caConfigMapRef` mounts its selected key at
`/etc/pig/postgres-ca/ca.pem` and sets `PGSSLROOTCERT`. Analyzer and maintenance Jobs
share the same ServiceAccount, native workload identity, labels, and CA mount.
Set `spec.nodeSelector` to restrict both to nodes configured for that identity.
Secrets, CA, and ServiceAccount revisions trigger revalidation and analyzer
restart, including while release updates are paused. A pending schema transition
can defer this restart as described below. PIG never edits these customer-owned objects.

S3, Azure Blob, and GCS use their native SDK credential chains, including workload
identity token refresh. Storage federation does not configure model access.
The model contract is OpenAI, Azure OpenAI, or Bedrock Mantle, with the supported
provider URL and API key or Bedrock SigV4 authentication. Terraform examples do
not grant model permissions or provision model endpoints.

## HTTPS ingress

The bootstrap chart always creates the collector Ingress; configure it through
Helm values or the rendered GitOps manifest. `PIGDeployment` has no endpoint
settings, and the supervisor has no ingress permissions. The analyzer Service
remains `ClusterIP`, named `<PIGDeployment name>-analyzer`. Network changes are
independent of analyzer releases and do not enter its configuration hash.
Configure DNS and TLS so enrolled hosts can reach the endpoint.

Cloud agents need an HTTPS endpoint reachable from their sandbox's network.
Claude Tag's Agent Proxy blocks private IP ranges even when the destination host
is allowed, so a cluster Service or a private VPN endpoint alone cannot serve Tag.
Configure a public HTTPS ingress for collectors that cannot reach a private
endpoint. Allow the exact hostname and required methods in the channel's
network policy. See [Claude Tag network access](https://claude.com/docs/claude-tag/admins/add-connections#restrict-by-path-or-method).
HTTPS reachability does not enroll an agent: the worker must also support cloud
enrollment, and the collector needs an authorized enrollment credential.

To use an ingress controller with a Kubernetes TLS Secret, configure the
[bootstrap chart values](examples/supervisor-values.yaml):

```yaml
ingress:
  serviceName: acme-analyzer
  hostname: pig.example.com
  ingressClassName: nginx
  tlsSecretName: pig-tls
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-body-size: "256m"
```

The operator supplies the ingress controller, DNS, HTTPS listener, and a valid
certificate for `pig.example.com`. `pig-tls` is a customer-managed TLS Secret in
the analyzer namespace. Omit `tlsSecretName` when the controller uses a certificate
configured through `ingress.annotations` or its own configuration, such as
[AWS ALB with ACM](examples/aws/README.md#https-through-an-application-load-balancer).
The generated Ingress retains the TLS hostname in either case. Disable HTTP or
redirect it to HTTPS through the controller's settings. Match its request limit
to the worker's trace batch limit, which defaults to 256 MiB.

The generated Ingress exposes only these exact paths. A customer gateway should
use the same list and may also restrict methods; Kubernetes Ingress routes paths
but does not filter HTTP methods.

| Method | Exact path | Purpose |
| --- | --- | --- |
| GET | `/healthz` | Process health |
| GET | `/v0/host-enrollment/policy` | Collector policy |
| POST | `/v0/host-enrollment/check-ins` | Collector check-in |
| POST | `/v0/cloud-enrollment/leases` | Cloud enrollment and lease renewal |
| POST | `/v0/traces/batches` | Trace upload |

Keep worker administration and hosted control-plane routes outside this list.
Forward collector authentication headers to the worker without logging their
values. TLS and gateway access controls supplement the worker's authentication.
Verify the certificate, health response, authenticated policy request, and trace
upload from the actual cloud sandbox before relying on collection. Readiness or
a successful local request does not prove cloud reachability or trace persistence.

Pausing or deleting a `PIGDeployment` does not remove the Helm-owned Ingress.
Its lifecycle belongs to the bootstrap release; uninstalling that release removes
it. Follow [the ownership handoff](UPGRADING.md) when upgrading an existing
installation, preserving the same Ingress object and removing its PIGDeployment
owner reference. Keep the currently accepted supervisor image and catalog in
bootstrap values before an ingress-only Helm upgrade, so it does not undo a
supervisor self-update. See the [bootstrap chart](charts/pig-supervisor/README.md)
and [manual chart's gateway configuration](charts/pig-trace-analyzer/README.md#optional-https-ingress).

## Automatic updates

`spec.release.channel: stable` selects the highest stable semantic version,
including major versions. `pinnedVersion` selects an exact published version;
`paused` halts release transitions at a safe checkpoint. An active migration is
allowed to finish before pause takes effect. A pause before the first install
blocks installation.

Before the migration checkpoint, pausing cancels a pending upgrade and keeps the
installed release available for configuration rotation, including without catalog
access. After that checkpoint, resume the transition before rotating configuration;
the old analyzer may no longer be compatible with the database. This restriction
also applies to older in-progress status that lacks a recorded migration boundary.
After selecting a forward repair for a transition that may have migrated, finish
the repair before using its rollback declarations to select an older release.

The supervisor records progress in CR status through preflight, quiesce,
migration, analyzer rollout, verification, and supervisor rollout. It scales the
existing analyzer to zero and waits for its Pods to exit before migration. It
starts a single analyzer replica afterwards. Jobs use immutable images and names
bound to the release, configuration, and saved transition ID. A new transition runs
fresh checks rather than reusing an earlier successful Job. PostgreSQL records
the Alembic revision in `pig_alembic_version`, historical checksums in
`pig_schema_migrations`, and release checkpoints in `pig_deployment_history`. A restart resumes the saved
transition; a failed Job retries after the operator corrects its cause.

Live checks include PostgreSQL version, schema compatibility, verified TLS,
transactional write access, and native object write/read access. Workload
availability is checked before `Ready=True`. A failed dependency check sets
`Blocked=True` with the Job to inspect. Change cloud resources through Terraform;
checks retry automatically. Declared Terraform capacity is never mirrored into
PIG or presented as observed free capacity.

A rollout that exceeds its Kubernetes progress deadline sets `Blocked=True`.
Correct the Pod failure or select a compatible repair release. An unpinned stable
policy can select a newer release after a failed rollout.

Rollback is available only when the current manifest explicitly lists the target
release digest in `rollbackTo` and both releases use the same schema revision.
If an incomplete update has reached migration, its target manifest controls this
check, even when the old release is still recorded as installed.
Otherwise choose a compatible forward repair release. A running migration cannot
be replaced by a repair release until it terminates. Database or object recovery
is an operator action, never an automatic destructive restore.

The 0.3.0 and 0.3.1 releases target schema revision 3, which adds instruction-source
provenance and includes the earlier native-location migration that preserves trace
data while removing the duplicate location column and synchronization objects.
Their `destructiveMigration: false` declaration does not permit restarting older
images. Use a schema-3-compatible image or forward repair after migration.

The 0.3.2 candidate targets schema revision 4. It adopts validated predecessor
schemas without resetting data and adds persistent canonical-object retries.
Use a schema-4-compatible image or forward repair after migration. The worker
requires the exact image schema before serving uploads or starting analyses.
Readiness checks storage separately from process liveness.

Optional `storage.postgres.migrationDsnSecretRef` supplies an owner credential
only to preflight and migration Jobs. The serving application credential can
remain restricted to data access. Both identities must reach the same database
with verified TLS. See [storage operations](STORAGE.md) for grants, retention,
and coordinated database and object recovery.

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
must contain `releaseDigest`, `deploymentUID` matching the PIGDeployment's `metadata.uid`,
`confirmedAt` with a timezone, `postgresRecoveryPoint`, and `objectRecoveryPoint`.
The timestamp must fall within the release's `recoveryMaxAgeHours`. The
supervisor checks it before starting a transition and immediately before starting
a new migration Job, including after a pause.

Read the UID with `kubectl get pigdeployment NAME -n NAMESPACE -o jsonpath='{.metadata.uid}'`.
Recreating the Kubernetes object requires a new confirmation, even when its name
and Promptless installation are unchanged. This UID is not a registration credential.

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

The [manual chart](charts/pig-trace-analyzer/README.md) is for operator-managed
releases. Its pre-upgrade migration hook requires the operator to quiesce the
existing analyzer before upgrading.
It does not enforce the supervisor's release confirmation ConfigMap. Supply a
compatible immutable analyzer image and a verified recovery point. Create the
shared analyzer and migration ServiceAccount before installing the chart. Do not
install the manual analyzer and a supervised analyzer for the same deployment.
