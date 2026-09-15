provider "aws" { region = var.region }

# A release checkout pins this module to the same reviewed commit.
module "pig" {
  source                     = "../../modules/aws"
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
  region                     = var.region
  eks_cluster_name           = var.eks_cluster_name
  oidc_provider_arn          = var.oidc_provider_arn
  vpc_id                     = var.vpc_id
  database_subnet_ids        = var.database_subnet_ids
  workload_security_group_id = var.workload_security_group_id
  trace_bucket_name          = var.trace_bucket_name
  postgres_instance_class    = var.postgres_instance_class
  postgres_storage_gib       = var.postgres_storage_gib
  postgres_max_storage_gib   = var.postgres_max_storage_gib
  postgres_multi_az          = var.postgres_multi_az
  deletion_protection        = var.deletion_protection
  kms_key_arn                = var.kms_key_arn
  trace_retention_days       = var.trace_retention_days
  tags                       = var.tags
}

output "deployment_configuration" {
  value = module.pig.deployment_configuration
}
