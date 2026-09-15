# Provision infrastructure for an existing cluster

Choose [AWS](aws/README.md), [Azure](azure/README.md), or [GCP](gcp/README.md).
Each example uses Terraform 1.11.4 and committed provider locks. Cluster creation,
state backend creation, DNS/TLS ingress, model endpoints, and Secret delivery
remain customer responsibilities.

Use a released source archive with its published checksum, or check out its
exact source commit. The example's relative module source resolves to that same
checkout; it does not depend on a separate unpublished module tag. To embed a
module in another root, use its Git URL with the release's full source commit as
`ref`, and commit your own provider lock file.

## Infrastructure workflow

1. Prepare an existing encrypted, private, versioned state backend with locking
   and access limited to infrastructure operators. Copy `backend.tfbackend.example`
   to a local backend configuration and replace its values. Do not commit state,
   credentials, or real `.tfvars` files.
2. Copy `terraform.tfvars.example` to a local `.tfvars` file and fill in the
   existing cluster/network details. Supply `TF_VAR_postgres_password` through
   your approved secret workflow. Terraform marks it sensitive, but the database
   password is still stored in encrypted remote state.
3. Run `terraform init -backend-config=backend.tfbackend -lockfile=readonly`, then
   review `terraform plan`. Apply the reviewed plan through your infrastructure
   workflow. No Terraform resource installs Helm or changes Kubernetes.
4. Read `terraform output -json deployment_configuration`. Build a hostname-
   verified PostgreSQL DSN in your secret system from its metadata and password.
   Deliver the database CA through a ConfigMap when required.

## Kubernetes handoff

1. Create the analyzer namespace and customer-owned `pig-analyzer` ServiceAccount.
   Apply the output's `service_account_annotations` to that account. Retain
   `pod_labels` in the PIGDeployment, especially Azure's workload-identity label.
2. Deliver the referenced credentials and CA ConfigMap. Configure existing ingress,
   DNS, and TLS for the analyzer endpoint. Allow the worker's PostgreSQL, object
   storage, hosted API, repository, and selected model traffic.
3. Bootstrap the published `pig-supervisor` chart once in `pig-system`, with
   `watchNamespace: pig` and its pinned chart version. If Flux performed bootstrap,
   suspend its HelmRelease before proceeding.
4. Adapt [pig-deployment.yaml](pig-deployment.yaml): paste exactly one native
   storage block from the output, set hosted installation/repository/model values,
   and apply it through GitOps. PIG manages its generated workloads thereafter.
5. Enroll a real host, ingest a trace, and verify the exact canonical object,
   succeeded analysis, and Dashboard synchronization. See [the contract](../CONTRACT.md).

Sizing and backup defaults are starting points to review against workload and
recovery requirements. Terraform owns every capacity change. Its output reports
provisioned metadata for the operator; PIG does not consume a Terraform capacity
inventory.
