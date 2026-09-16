output "deployment_configuration" {
  description = "Nonsecret inputs for customer Secret delivery and PIGDeployment."
  value = {
    postgres                    = { host = azurerm_postgresql_flexible_server.postgres.fqdn, port = 5432, database = var.postgres_database, username = var.postgres_user, sslmode = "verify-full", provisioned_storage_gib = var.postgres_storage_mb / 1024, backup_retention_days = var.backup_retention_days }
    storage                     = { azureBlob = { accountURL = azurerm_storage_account.traces.primary_blob_endpoint, container = azurerm_storage_container.traces.name, prefix = local.prefix } }
    service_account_annotations = { "azure.workload.identity/client-id" = azurerm_user_assigned_identity.analyzer.client_id }
    pod_labels                  = { "azure.workload.identity/use" = "true" }
  }
}
