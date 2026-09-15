output "deployment_configuration" {
  description = "Nonsecret inputs for customer Secret delivery and PIGDeployment."
  value = {
    postgres                    = { host = aws_db_instance.postgres.address, port = 5432, database = var.postgres_database, username = var.postgres_user, sslmode = "verify-full", provisioned_storage_gib = var.postgres_storage_gib, backup_retention_days = var.backup_retention_days }
    storage                     = { s3 = { region = var.region, bucket = aws_s3_bucket.traces.id, prefix = local.prefix } }
    service_account_annotations = { "eks.amazonaws.com/role-arn" = aws_iam_role.analyzer.arn }
    pod_labels                  = {}
  }
}
