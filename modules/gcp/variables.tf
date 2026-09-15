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

variable "project_id" {
  type        = string
  description = "Existing GCP project with Cloud SQL, IAM, and Storage APIs enabled."
}

variable "region" {
  type        = string
  description = "Region containing the existing GKE cluster."
}

variable "workload_identity_pool" {
  type        = string
  description = "Existing GKE workload pool, usually PROJECT_ID.svc.id.goog."
}

variable "network_id" {
  type        = string
  description = "Existing VPC self-link with private services access already established."
}

variable "allocated_ip_range" {
  type        = string
  description = "Existing private services access allocated range name."
}

variable "trace_bucket_name" {
  type        = string
  description = "Globally unique private GCS bucket name."
}

variable "postgres_tier" {
  type        = string
  description = "Cloud SQL compute tier."
  default     = "db-custom-2-7680"
}

variable "postgres_storage_gib" {
  type        = number
  description = "Provisioned database storage in GiB."
  default     = 100
  validation {
    condition     = var.postgres_storage_gib >= 10
    error_message = "Cloud SQL storage must be at least 10 GiB."
  }
}

variable "postgres_availability_type" {
  type        = string
  description = "REGIONAL for HA or ZONAL."
  default     = "REGIONAL"
  validation {
    condition     = contains(["REGIONAL", "ZONAL"], var.postgres_availability_type)
    error_message = "Choose REGIONAL or ZONAL."
  }
}

variable "deletion_protection" {
  type        = bool
  description = "Cloud SQL API and Terraform deletion protection."
  default     = true
}

variable "database_kms_key" {
  type        = string
  description = "Optional existing regional Cloud SQL CMEK with grants prepared by the customer."
  default     = null
}

variable "storage_kms_key" {
  type        = string
  description = "Optional existing bucket CMEK with grants prepared by the customer."
  default     = null
}

variable "trace_retention_days" {
  type        = number
  description = "Expiration for live trace objects; 0 retains them indefinitely."
  default     = 0
}

variable "labels" {
  type        = map(string)
  description = "GCP labels."
  default     = {}
}
