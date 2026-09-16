locals { prefix = trim(var.trace_prefix, "/") }
resource "azurerm_user_assigned_identity" "analyzer" {
  name                = "${var.name}-analyzer"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}
resource "azurerm_federated_identity_credential" "analyzer" {
  name                = "${var.name}-analyzer"
  parent_id           = azurerm_user_assigned_identity.analyzer.id
  resource_group_name = var.resource_group_name
  audience            = ["api://AzureADTokenExchange"]
  issuer              = var.oidc_issuer_url
  subject             = "system:serviceaccount:${var.namespace}:${var.service_account_name}"
}
resource "azurerm_storage_account" "traces" {
  name                              = var.storage_account_name
  resource_group_name               = var.resource_group_name
  location                          = var.location
  account_tier                      = "Standard"
  account_replication_type          = var.storage_replication_type
  min_tls_version                   = "TLS1_2"
  public_network_access_enabled     = false
  allow_nested_items_to_be_public   = false
  shared_access_key_enabled         = false
  default_to_oauth_authentication   = true
  infrastructure_encryption_enabled = true
  network_rules {
    default_action = "Deny"
    bypass         = ["None"]
  }
  blob_properties {
    versioning_enabled            = true
    change_feed_enabled           = true
    change_feed_retention_in_days = var.object_recovery_days
    delete_retention_policy { days = var.object_recovery_days }
    container_delete_retention_policy { days = var.object_recovery_days }
    restore_policy { days = var.object_recovery_days - 1 }
  }
  tags = var.tags
  lifecycle { prevent_destroy = true }
}
resource "azurerm_storage_container" "traces" {
  name                  = var.container_name
  storage_account_id    = azurerm_storage_account.traces.id
  container_access_type = "private"
  lifecycle { prevent_destroy = true }
}
resource "azurerm_storage_management_policy" "traces" {
  storage_account_id = azurerm_storage_account.traces.id
  rule {
    name    = "trace-version-recovery"
    enabled = true
    filters {
      prefix_match = ["${azurerm_storage_container.traces.name}/${local.prefix}/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      version {
        delete_after_days_since_creation = var.object_recovery_days
      }
    }
  }
}
resource "azurerm_private_endpoint" "blob" {
  name                = "${var.name}-blob"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.private_endpoint_subnet_id
  private_service_connection {
    name                           = "${var.name}-blob"
    private_connection_resource_id = azurerm_storage_account.traces.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "blob"
    private_dns_zone_ids = [var.blob_private_dns_zone_id]
  }
  tags = var.tags
}
resource "azurerm_role_assignment" "traces" {
  scope                = azurerm_storage_container.traces.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.analyzer.principal_id
  # The built-in role is restricted to this dedicated container, never the account or subscription.
}
resource "azurerm_postgresql_flexible_server" "postgres" {
  name                          = var.name
  resource_group_name           = var.resource_group_name
  location                      = var.location
  version                       = var.postgres_version
  administrator_login           = var.postgres_user
  administrator_password        = var.postgres_password
  delegated_subnet_id           = var.postgres_subnet_id
  private_dns_zone_id           = var.postgres_private_dns_zone_id
  public_network_access_enabled = false
  sku_name                      = var.postgres_sku
  storage_mb                    = var.postgres_storage_mb
  auto_grow_enabled             = false
  backup_retention_days         = var.backup_retention_days
  geo_redundant_backup_enabled  = var.geo_redundant_backup
  dynamic "high_availability" {
    for_each = var.postgres_ha_mode == null ? [] : [var.postgres_ha_mode]
    content { mode = high_availability.value }
  }
  tags = var.tags
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [zone, high_availability[0].standby_availability_zone]
  }
}
resource "azurerm_postgresql_flexible_server_database" "pig" {
  name      = var.postgres_database
  server_id = azurerm_postgresql_flexible_server.postgres.id
  charset   = "UTF8"
  collation = "en_US.utf8"
  lifecycle { prevent_destroy = true }
}
resource "azurerm_postgresql_flexible_server_configuration" "tls" {
  name      = "require_secure_transport"
  server_id = azurerm_postgresql_flexible_server.postgres.id
  value     = "on"
}
