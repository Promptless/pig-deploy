provider "azurerm" {
  features {
    storage {
      # Account creation precedes its private endpoint, so skip data-plane probes.
      # https://github.com/hashicorp/terraform-provider-azurerm/blob/v4.36.0/internal/services/storage/storage_account_resource.go#L1520-L1533
      data_plane_available = false
    }
  }
  subscription_id                 = var.subscription_id
  storage_use_azuread             = true
  resource_provider_registrations = "none"
}

# A release checkout pins this module to the same reviewed commit.
module "pig" {
  source                       = "../../modules/azure"
  name                         = var.name
  namespace                    = var.namespace
  service_account_name         = var.service_account_name
  trace_prefix                 = var.trace_prefix
  postgres_database            = var.postgres_database
  postgres_user                = var.postgres_user
  postgres_password            = var.postgres_password
  postgres_version             = var.postgres_version
  backup_retention_days        = var.backup_retention_days
  object_recovery_days         = var.object_recovery_days
  location                     = var.location
  resource_group_name          = var.resource_group_name
  oidc_issuer_url              = var.oidc_issuer_url
  postgres_subnet_id           = var.postgres_subnet_id
  postgres_private_dns_zone_id = var.postgres_private_dns_zone_id
  private_endpoint_subnet_id   = var.private_endpoint_subnet_id
  blob_private_dns_zone_id     = var.blob_private_dns_zone_id
  storage_account_name         = var.storage_account_name
  container_name               = var.container_name
  postgres_sku                 = var.postgres_sku
  postgres_storage_mb          = var.postgres_storage_mb
  postgres_ha_mode             = var.postgres_ha_mode
  geo_redundant_backup         = var.geo_redundant_backup
  storage_replication_type     = var.storage_replication_type
  tags                         = var.tags
}

output "deployment_configuration" {
  value = module.pig.deployment_configuration
}
