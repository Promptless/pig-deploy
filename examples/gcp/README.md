# Existing GKE cluster

This cloud is experimental in 0.3.0. Its deployment module and native storage
adapter are implemented, but the first release validates AWS clean installation
and canonical acceptance. See [release operations](../../RELEASING.md).

Follow the [shared workflow](../README.md) with Google provider 6.49.0. Supply an
existing project with Cloud SQL, IAM, and Storage APIs enabled, a GKE workload
identity pool, and a VPC with private services access and its allocated range
already configured. Enable the GKE metadata server on the analyzer node pool.
Cloud SQL private IP, Google APIs, and the selected model endpoint must be
reachable through customer-managed routes and egress.

On GKE Standard, set `spec.nodeSelector` in `PIGDeployment` to place the analyzer
and every maintenance Job on nodes with the GKE metadata server:

```yaml
nodeSelector:
  iam.gke.io/gke-metadata-server-enabled: "true"
```

For the manual chart, set the same `nodeSelector` in Helm values. Omit this
selector on Autopilot. Every Autopilot node supports workload identity, and
Autopilot rejects this selector. See the [GKE workload identity setup](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/workload-identity#verify_the_workload_identity_federation_for_gke_setup).

The module binds the exact Kubernetes namespace/ServiceAccount to a dedicated
Google service account. GCS Object User is restricted by an IAM condition to the
dedicated trace prefix. The workload receives no Cloud SQL admin or infrastructure
control role. Model access is configured independently.

Defaults provision PostgreSQL 16, two vCPUs/7.5 GiB RAM, regional HA, 100 GiB SSD,
14 retained daily backups, and seven days of transaction-log recovery. Storage
autogrowth is disabled: Terraform owns disk sizing. The private GCS bucket has
versioning, 90-day soft delete, and noncurrent-version retention. Current objects
are retained unless `trace_retention_days` is set. Database and bucket destruction
are protected. Optional CMEK keys require customer-prepared service-agent grants.
Review the [inputs](variables.tf) and [module](../../modules/gcp/main.tf).

The Cloud SQL instance uses Google-managed regional CA service. Deliver its
regional CA bundle, then use both output `host` (certificate DNS identity) and
`hostaddr` (private IP) in the libpq DSN with `sslmode=verify-full`. This avoids
relying on an IP certificate name. Confirm actual certificate/DNS behavior in the
cloud acceptance environment before claiming GCP acceptance or promoting GCP
support beyond experimental.

State uses an existing encrypted, private, versioned GCS bucket with native
locking and the configured state CMEK. State and application CMEK grants are
separate.
