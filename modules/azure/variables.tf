variable "name" {
  type        = string
  description = "Unique resource name prefix for this installation."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,19}[a-z0-9]$", var.name))
    error_message = "Use a lowercase resource prefix of 3-21 characters."
  }
}

variable "namespace" {
  type        = string
  description = "Existing analyzer namespace."
  default     = "pig"
}

variable "service_account_name" {
  type        = string
  description = "Customer-owned Kubernetes ServiceAccount."
  default     = "pig-analyzer"
}

variable "trace_prefix" {
  type        = string
  description = "Dedicated object key prefix, without leading or trailing slash."
  default     = "trace-objects"
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+([/.-][A-Za-z0-9_-]+)*$", var.trace_prefix))
    error_message = "Use a nonempty relative trace prefix with no empty or dot path segments."
  }
}

variable "postgres_database" {
  type        = string
  description = "Dedicated database name."
  default     = "pig"
}

variable "postgres_user" {
  type        = string
  description = "Dedicated instance administrator; deliver its DSN through a Secret."
  default     = "pig"
}

variable "postgres_password" {
  type        = string
  description = "Password from the approved secret workflow; Terraform state must be protected."
  sensitive   = true
}

variable "postgres_version" {
  type        = string
  description = "Supported PostgreSQL major version."
  default     = "16"
  validation {
    condition     = contains(["15", "16", "17"], var.postgres_version)
    error_message = "This release supports PostgreSQL 15, 16, or 17; confirm regional availability."
  }
}

variable "backup_retention_days" {
  type        = number
  description = "Database point-in-time recovery retention in days."
  default     = 14
  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35
    error_message = "Choose 7-35 retained daily backups (PITR policy is provider-specific)."
  }
}

variable "object_recovery_days" {
  type        = number
  description = "Retention for overwritten and deleted object versions."
  default     = 90
  validation {
    condition     = var.object_recovery_days >= 7 && var.object_recovery_days <= 90
    error_message = "Choose 7-90 days for object recovery."
  }
}

variable "location" {
  type        = string
  description = "Azure location for dedicated resources."
}

variable "resource_group_name" {
  type        = string
  description = "Existing customer resource group."
}

variable "oidc_issuer_url" {
  type        = string
  description = "OIDC issuer of the existing AKS cluster with workload identity enabled."
}

variable "postgres_subnet_id" {
  type        = string
  description = "Existing subnet delegated to Microsoft.DBforPostgreSQL/flexibleServers."
}

variable "postgres_private_dns_zone_id" {
  type        = string
  description = "Existing linked private PostgreSQL DNS zone."
}

variable "private_endpoint_subnet_id" {
  type        = string
  description = "Existing subnet for the Blob private endpoint."
}

variable "blob_private_dns_zone_id" {
  type        = string
  description = "Existing linked privatelink.blob.core.windows.net DNS zone."
}

variable "storage_account_name" {
  type        = string
  description = "Globally unique 3-24 lowercase alphanumeric account name."
  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.storage_account_name))
    error_message = "Use a 3-24 lowercase alphanumeric storage account name."
  }
}

variable "container_name" {
  type        = string
  description = "Private trace container."
  default     = "traces"
}

variable "postgres_sku" {
  type        = string
  description = "Flexible Server compute SKU."
  default     = "GP_Standard_D2s_v3"
}

variable "postgres_storage_mb" {
  type        = number
  description = "Provisioned database storage in MiB."
  default     = 131072
}

variable "postgres_ha_mode" {
  type        = string
  description = "ZoneRedundant, SameZone, or null for no standby."
  default     = "ZoneRedundant"
  validation {
    condition     = var.postgres_ha_mode == null ? true : contains(["SameZone", "ZoneRedundant"], var.postgres_ha_mode)
    error_message = "Choose SameZone, ZoneRedundant, or null."
  }
}

variable "geo_redundant_backup" {
  type        = bool
  description = "Enable geographic backup replication on creation."
  default     = false
}

variable "storage_replication_type" {
  type        = string
  description = "Storage replication policy; use a supported regional option."
  default     = "ZRS"
}

variable "tags" {
  type        = map(string)
  description = "Resource tags."
  default     = {}
}
