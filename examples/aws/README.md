# Existing EKS cluster

Follow the [shared workflow](../README.md) with AWS provider 6.10.0. Supply an
existing EKS cluster, its IAM OIDC provider, VPC, two private database subnets in
different availability zones, and the security group used by analyzer traffic.
The module verifies the EKS issuer and binds IRSA to exactly the configured
namespace and ServiceAccount.

The Terraform runner needs the cloud control permissions for the reviewed plan.
The analyzer role gets object read/write under its dedicated S3 prefix and, when
configured, access to the supplied KMS key. It receives no RDS or infrastructure
control permissions. S3 must be reachable through the existing network's gateway
endpoint or approved egress. PostgreSQL ingress is limited to the supplied
workload security group.

Defaults provision private PostgreSQL 16 on `db.t4g.medium`, Multi-AZ, 100 GiB
with a 200 GiB storage growth limit, 14 days of database recovery, and versioned
S3 with 90 days of noncurrent object recovery. Current objects are retained unless
`trace_retention_days` is set. RDS uses encrypted storage and deletion protection;
Terraform prevents database and bucket destruction. Review the full
[inputs](variables.tf) and [module](../../modules/aws/main.tf).

Use the current RDS regional/root CA bundle in your customer CA ConfigMap. Build
the DSN with the output hostname and `sslmode=verify-full`. AWS state uses the
existing encrypted S3 backend with `use_lockfile=true`; operators need lock-object
permissions as well as state access. Backend and application KMS keys are separate.
