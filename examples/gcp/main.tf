provider "google" {
  project = var.project_id
  region  = var.region
}

# A release checkout pins this module to the same reviewed commit.
module "pig" {
  source                     = "../../modules/gcp"
  name                       = var.name
  namespace                  = var.namespace
  service_account_name       = var.service_account_name
  trace_prefix               = var.trace_prefix
  postgres_database          = var.postgres_database
  postgres_user              = var.postgres_user
  postgres_password          = var.postgres_password
  postgres_version           = var.postgres_version
  backup_retention_days      = var.backup_retention_days
  object_recovery_days       = var.object_recovery_days
  project_id                 = var.project_id
  region                     = var.region
  workload_identity_pool     = var.workload_identity_pool
  network_id                 = var.network_id
  allocated_ip_range         = var.allocated_ip_range
  trace_bucket_name          = var.trace_bucket_name
  postgres_tier              = var.postgres_tier
  postgres_storage_gib       = var.postgres_storage_gib
  postgres_availability_type = var.postgres_availability_type
  deletion_protection        = var.deletion_protection
  database_kms_key           = var.database_kms_key
  storage_kms_key            = var.storage_kms_key
  trace_retention_days       = var.trace_retention_days
  labels                     = var.labels
}

output "deployment_configuration" {
  value = module.pig.deployment_configuration
}
