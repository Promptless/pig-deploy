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

## HTTPS through an Application Load Balancer

Install the [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/deploy/installation/)
with its own IAM role, and provision an ACM certificate for the analyzer hostname
in the ALB's region. These are external prerequisites; this Terraform module does
not provision them. For the supervisor installation, replace `spec.endpoint` in
[the deployment example](../pig-deployment.yaml) with:

```yaml
endpoint:
  hostname: pig.example.com
  ingressClassName: alb
  ingressAnnotations:
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/certificate-arn: REPLACE_ACM_CERTIFICATE_ARN
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'
    alb.ingress.kubernetes.io/healthcheck-path: /healthz
```

Omit `tlsSecretName`: ALB terminates TLS using the ACM certificate. Choose an
internal scheme if all enrolled hosts can reach the private endpoint. Point the
hostname's DNS record at the provisioned ALB after its targets are healthy, then
verify HTTPS with the hostname before enrolling a host.
