mock_provider "google" {
  override_during = plan
  mock_resource "google_sql_database_instance" {
    defaults = { dns_names = [{ name = "db.example.internal", connection_type = "PRIVATE_SERVICES_ACCESS", dns_scope = "INSTANCE" }] }
  }
}
variables {
  name                   = "pig-test"
  postgres_password      = "mock-test-only"
  project_id             = "pig-test-project"
  region                 = "us-central1"
  workload_identity_pool = "pig-test-project.svc.id.goog"
  network_id             = "projects/pig-test-project/global/networks/existing"
  allocated_ip_range     = "existing-range"
  trace_bucket_name      = "pig-test-traces"
}
run "private_native_contract" {
  command = plan
  assert {
    condition     = !google_sql_database_instance.postgres.settings[0].ip_configuration[0].ipv4_enabled && google_sql_database_instance.postgres.settings[0].ip_configuration[0].ssl_mode == "ENCRYPTED_ONLY"
    error_message = "PostgreSQL must be private with mandatory TLS."
  }
  assert {
    condition     = google_sql_database_instance.postgres.settings[0].backup_configuration[0].point_in_time_recovery_enabled && google_sql_database_instance.postgres.deletion_protection
    error_message = "PITR and deletion protection must remain enabled."
  }
  assert {
    condition     = google_storage_bucket.traces.public_access_prevention == "enforced" && google_storage_bucket.traces.versioning[0].enabled
    error_message = "Trace objects must be private and recoverable."
  }
  assert {
    condition     = google_service_account_iam_member.workload.member == "serviceAccount:pig-test-project.svc.id.goog[pig/pig-analyzer]"
    error_message = "Federation must bind the customer ServiceAccount."
  }
  assert {
    condition     = google_storage_bucket_iam_member.traces.condition[0].expression == "resource.name.startsWith('projects/_/buckets/pig-test-traces/objects/trace-objects/')"
    error_message = "Native object permissions must be prefix scoped."
  }
}
run "reject_escaping_prefix" {
  command = plan
  variables { trace_prefix = "../other" }
  expect_failures = [var.trace_prefix]
}
