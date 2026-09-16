mock_provider "azurerm" {
  override_during = plan
}
variables {
  name                         = "pig-test"
  postgres_password            = "mock-test-only"
  location                     = "eastus"
  resource_group_name          = "existing"
  oidc_issuer_url              = "https://eastus.oic.prod-aks.azure.com/tenant/test/"
  postgres_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing/providers/Microsoft.Network/virtualNetworks/existing/subnets/postgres"
  postgres_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing/providers/Microsoft.Network/privateDnsZones/pig.postgres.database.azure.com"
  private_endpoint_subnet_id   = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing/providers/Microsoft.Network/virtualNetworks/existing/subnets/private"
  blob_private_dns_zone_id     = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/existing/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net"
  storage_account_name         = "pigtesttraces"
}
run "private_native_contract" {
  command = plan
  assert {
    condition     = !azurerm_storage_account.traces.public_network_access_enabled && !azurerm_storage_account.traces.shared_access_key_enabled
    error_message = "Blob access must be private workload identity."
  }
  assert {
    condition     = azurerm_federated_identity_credential.analyzer.subject == "system:serviceaccount:pig:pig-analyzer"
    error_message = "Federation must bind the customer ServiceAccount."
  }
  assert {
    condition     = !azurerm_postgresql_flexible_server.postgres.public_network_access_enabled && azurerm_postgresql_flexible_server.postgres.backup_retention_days == 14
    error_message = "PostgreSQL must use private networking and backups."
  }
  assert {
    condition     = azurerm_storage_account.traces.blob_properties[0].versioning_enabled && azurerm_storage_account.traces.blob_properties[0].restore_policy[0].days == 89
    error_message = "Canonical overwrites require recoverable object versions."
  }
  assert {
    condition     = output.deployment_configuration.pod_labels["azure.workload.identity/use"] == "true"
    error_message = "Both analyzer and maintenance Jobs need the workload identity label."
  }
  assert {
    condition     = azurerm_storage_management_policy.traces.rule[0].enabled && azurerm_storage_management_policy.traces.rule[0].actions[0].version[0].delete_after_days_since_creation == 90
    error_message = "Previous trace versions must expire after the configured recovery interval."
  }
  assert {
    condition     = azurerm_storage_management_policy.traces.rule[0].filters[0].prefix_match == toset(["traces/trace-objects/"]) && azurerm_storage_management_policy.traces.rule[0].filters[0].blob_types == toset(["blockBlob"])
    error_message = "Version expiration must be limited to block blobs under the trace prefix."
  }
  assert {
    condition     = length(azurerm_storage_management_policy.traces.rule[0].actions[0].base_blob) == 0
    error_message = "Lifecycle expiration must preserve current trace objects."
  }
}
run "custom_version_recovery" {
  command = plan
  variables {
    container_name       = "customer-traces"
    trace_prefix         = "acme/traces"
    object_recovery_days = 14
  }
  assert {
    condition     = azurerm_storage_management_policy.traces.rule[0].filters[0].prefix_match == toset(["customer-traces/acme/traces/"])
    error_message = "Version expiration must follow the configured container and trace prefix."
  }
  assert {
    condition     = azurerm_storage_management_policy.traces.rule[0].actions[0].version[0].delete_after_days_since_creation == 14 && azurerm_storage_account.traces.blob_properties[0].delete_retention_policy[0].days == 14 && azurerm_storage_account.traces.blob_properties[0].restore_policy[0].days == 13
    error_message = "Version expiration and recovery windows must follow object_recovery_days."
  }
}
run "reject_escaping_prefix" {
  command = plan
  variables { trace_prefix = "../other" }
  expect_failures = [var.trace_prefix]
}
