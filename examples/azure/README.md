# Existing AKS cluster

This cloud is experimental in 0.3.0. Its deployment module and native storage
adapter are implemented, but the first release validates AWS clean installation
and canonical acceptance. See [release operations](../../RELEASING.md).

Follow the [shared workflow](../README.md) with AzureRM provider 4.36.0. Supply an
existing resource group, AKS OIDC issuer with workload identity enabled, delegated
PostgreSQL subnet, private endpoint subnet, and private PostgreSQL/Blob DNS zones
already linked to the cluster VNet. Required resource providers must already be
registered; the example does not register them automatically.

The Terraform runner needs Azure Resource Manager permissions and access to the
state backend. Keep `features.storage.data_plane_available = false` in the
[provider configuration](main.tf) so account creation can finish before the
private endpoint exists. Application storage resources use control-plane APIs;
analyzer workloads access blobs through the private endpoint. The storage account
disables public networking and shared keys. Federated identity binds the analyzer
identity to exactly the configured namespace and ServiceAccount; Blob Data
Contributor is scoped to the dedicated container. Retain the output
`azure.workload.identity/use: "true"` pod label and client-ID ServiceAccount
annotation for both analyzer and maintenance Jobs.

Defaults provision PostgreSQL 16 on `GP_Standard_D2s_v3`, 128 GiB, zone-redundant
HA, and 14-day backup retention. Storage uses ZRS, blob versioning, 90-day soft
delete, and 89-day point-in-time restore. Previous trace versions become eligible
for lifecycle deletion 90 days after creation; soft delete retains deleted
versions for another 90 days. Both intervals use `object_recovery_days`. Current
trace objects are retained indefinitely. See [Azure's version and soft-delete
semantics](https://learn.microsoft.com/en-us/azure/storage/blobs/soft-delete-blob-overview#blob-soft-delete-and-versioning).
Restore retention must be shorter than delete retention. Terraform prevents
database and account destruction. Review regional HA/replication support and the
full [inputs](variables.tf) and [module](../../modules/azure/main.tf).

Deliver a current PostgreSQL CA bundle and use its output hostname with
`sslmode=verify-full`. The existing Azure state account must enforce encryption,
private access, and Azure AD authorization. The `azurerm` backend uses blob lease
locking and `use_azuread_auth=true`; no state account key is required.
