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

variable "region" {
  type        = string
  description = "AWS region containing the existing EKS cluster and VPC."
}

variable "eks_cluster_name" {
  type        = string
  description = "Existing EKS cluster; this module never creates or changes it."
}

variable "oidc_provider_arn" {
  type        = string
  description = "Existing IAM OIDC provider for the EKS issuer."
}

variable "vpc_id" {
  type        = string
  description = "Existing VPC containing EKS and the database subnets."
}

variable "database_subnet_ids" {
  type        = list(string)
  description = "Existing private DB subnets in at least two availability zones."
  validation {
    condition     = length(var.database_subnet_ids) >= 2
    error_message = "Provide private database subnets across at least two availability zones."
  }
}

variable "workload_security_group_id" {
  type        = string
  description = "Existing security group actually used by analyzer pod traffic."
}

variable "trace_bucket_name" {
  type        = string
  description = "Globally unique private S3 bucket name."
}

variable "postgres_instance_class" {
  type        = string
  description = "RDS compute class."
  default     = "db.t4g.medium"
}

variable "postgres_storage_gib" {
  type        = number
  description = "Provisioned database storage in GiB."
  default     = 100
  validation {
    condition     = var.postgres_storage_gib >= 20
    error_message = "RDS storage must be at least 20 GiB."
  }
}

variable "postgres_max_storage_gib" {
  type        = number
  description = "Maximum managed storage growth in GiB; 0 disables autoscaling."
  default     = 200
}

variable "postgres_multi_az" {
  type        = bool
  description = "Whether RDS uses a Multi-AZ standby."
  default     = true
}

variable "deletion_protection" {
  type        = bool
  description = "RDS API deletion protection."
  default     = true
}

variable "kms_key_arn" {
  type        = string
  description = "Optional existing KMS key allowed for both RDS and S3; null uses service-managed encryption."
  default     = null
}

variable "trace_retention_days" {
  type        = number
  description = "Expiration for current trace objects; 0 retains current objects indefinitely."
  default     = 0
}

variable "tags" {
  type        = map(string)
  description = "Resource tags."
  default     = {}
}
