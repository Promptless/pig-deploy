output "deployment_configuration" {
  description = "Nonsecret inputs. Use host and hostaddr together with the regional CA bundle for verify-full."
  value = {
    postgres                    = { host = one(local.private_dns_names), hostaddr = google_sql_database_instance.postgres.private_ip_address, port = 5432, database = var.postgres_database, username = var.postgres_user, sslmode = "verify-full", provisioned_storage_gib = var.postgres_storage_gib, backup_retention_days = var.backup_retention_days, point_in_time_recovery_days = 7 }
    storage                     = { gcs = { bucket = google_storage_bucket.traces.name, prefix = local.prefix } }
    service_account_annotations = { "iam.gke.io/gcp-service-account" = google_service_account.analyzer.email }
    pod_labels                  = {}
  }
}
