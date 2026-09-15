# Existing AKS cluster

Follow the [shared workflow](../README.md) with AzureRM provider 4.36.0. Supply an
existing resource group, AKS OIDC issuer with workload identity enabled, delegated
PostgreSQL subnet, private endpoint subnet, and private PostgreSQL/Blob DNS zones
already linked to the cluster VNet. Required resource providers must already be
registered; the example does not register them automatically.

The Terraform runner must reach the Blob private endpoint for storage data-plane
operations and have the required Azure AD permissions. The storage account
disables public networking and shared keys. Federated identity binds the analyzer
identity to exactly the configured namespace and ServiceAccount; Blob Data
Contributor is scoped to the dedicated container. Retain the output
`azure.workload.identity/use: "true"` pod label and client-ID ServiceAccount
annotation for both analyzer and maintenance Jobs.

Defaults provision PostgreSQL 16 on `GP_Standard_D2s_v3`, 128 GiB, zone-redundant
HA, and 14-day backup retention. Storage uses ZRS, blob versioning, 90-day soft
delete, and 89-day point-in-time restore. Recovery intervals follow Azure's rule
that restore retention must be shorter than delete retention. Terraform prevents
database and account destruction. Review regional HA/replication support and the
full [inputs](variables.tf) and [module](../../modules/azure/main.tf).

Deliver a current PostgreSQL CA bundle and use its output hostname with
`sslmode=verify-full`. The existing Azure state account must enforce encryption,
private access, and Azure AD authorization. The `azurerm` backend uses blob lease
locking and `use_azuread_auth=true`; no state account key is required.
