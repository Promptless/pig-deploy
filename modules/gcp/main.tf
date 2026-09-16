locals {
  prefix            = trim(var.trace_prefix, "/")
  private_dns_names = [for entry in google_sql_database_instance.postgres.dns_names : entry.name if entry.connection_type == "PRIVATE_SERVICES_ACCESS"]
}
resource "google_service_account" "analyzer" {
  project      = var.project_id
  account_id   = "${var.name}-analyzer"
  display_name = "PIG native trace data access"
}
resource "google_service_account_iam_member" "workload" {
  service_account_id = google_service_account.analyzer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.workload_identity_pool}[${var.namespace}/${var.service_account_name}]"
}
resource "google_storage_bucket" "traces" {
  project                     = var.project_id
  name                        = var.trace_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  soft_delete_policy { retention_duration_seconds = min(var.object_recovery_days, 90) * 86400 }
  lifecycle_rule {
    condition { days_since_noncurrent_time = var.object_recovery_days }
    action { type = "Delete" }
  }
  dynamic "lifecycle_rule" {
    for_each = var.trace_retention_days > 0 ? [var.trace_retention_days] : []
    content {
      condition {
        age        = lifecycle_rule.value
        with_state = "LIVE"
      }
      action { type = "Delete" }
    }
  }
  dynamic "encryption" {
    for_each = var.storage_kms_key == null ? [] : [var.storage_kms_key]
    content { default_kms_key_name = encryption.value }
  }
  labels = var.labels
  lifecycle { prevent_destroy = true }
}
resource "google_storage_bucket_iam_member" "traces" {
  bucket = google_storage_bucket.traces.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.analyzer.email}"
  condition {
    title       = "dedicated-trace-prefix"
    description = "Object data only under the PIG prefix"
    expression  = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.traces.name}/objects/${local.prefix}/')"
  }
}
resource "google_sql_database_instance" "postgres" {
  project             = var.project_id
  name                = var.name
  region              = var.region
  database_version    = "POSTGRES_${var.postgres_version}"
  deletion_protection = var.deletion_protection
  encryption_key_name = var.database_kms_key
  settings {
    edition                     = "ENTERPRISE"
    tier                        = var.postgres_tier
    availability_type           = var.postgres_availability_type
    disk_type                   = "PD_SSD"
    disk_size                   = var.postgres_storage_gib
    disk_autoresize             = false
    deletion_protection_enabled = var.deletion_protection
    ip_configuration {
      ipv4_enabled       = false
      private_network    = var.network_id
      allocated_ip_range = var.allocated_ip_range
      ssl_mode           = "ENCRYPTED_ONLY"
      server_ca_mode     = "GOOGLE_MANAGED_CAS_CA"
    }
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = var.backup_retention_days
        retention_unit   = "COUNT"
      }
    }
    retain_backups_on_delete = true
    user_labels              = var.labels
  }
  lifecycle { prevent_destroy = true }
}
resource "google_sql_database" "pig" {
  project  = var.project_id
  name     = var.postgres_database
  instance = google_sql_database_instance.postgres.name
  lifecycle { prevent_destroy = true }
}
resource "google_sql_user" "pig" {
  project  = var.project_id
  name     = var.postgres_user
  instance = google_sql_database_instance.postgres.name
  password = var.postgres_password
}
