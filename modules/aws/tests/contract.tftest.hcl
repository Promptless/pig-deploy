mock_provider "aws" {
  override_during = plan
  mock_data "aws_eks_cluster" {
    defaults = { identity = [{ oidc = [{ issuer = "https://oidc.eks.us-east-2.amazonaws.com/id/TEST" }] }] }
  }
  mock_resource "aws_s3_bucket" { defaults = { arn = "arn:aws:s3:::pig-test-traces", id = "pig-test-traces" } }
}
variables {
  name                       = "pig-test"
  postgres_password          = "mock-test-only"
  region                     = "us-east-2"
  eks_cluster_name           = "existing"
  oidc_provider_arn          = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-2.amazonaws.com/id/TEST"
  vpc_id                     = "vpc-12345678"
  database_subnet_ids        = ["subnet-12345678", "subnet-87654321"]
  workload_security_group_id = "sg-12345678"
  trace_bucket_name          = "pig-test-traces"
}
run "private_native_contract" {
  command = plan
  assert {
    condition     = !aws_db_instance.postgres.publicly_accessible && aws_db_instance.postgres.storage_encrypted && aws_db_instance.postgres.deletion_protection
    error_message = "Database must be private, encrypted, and protected."
  }
  assert {
    condition     = aws_db_instance.postgres.backup_retention_period == 14 && !aws_db_instance.postgres.allow_major_version_upgrade
    error_message = "Backups and explicit major changes must remain enabled."
  }
  assert {
    condition     = jsondecode(aws_iam_role.analyzer.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-east-2.amazonaws.com/id/TEST:sub"] == "system:serviceaccount:pig:pig-analyzer"
    error_message = "Federation must bind the customer ServiceAccount."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.traces.policy).Statement[0].Action == ["s3:GetObject", "s3:PutObject"]
    error_message = "Analyzer must receive object data permissions only."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.traces.policy).Statement[0].Resource == "arn:aws:s3:::pig-test-traces/trace-objects/*"
    error_message = "Object policy must be prefix scoped."
  }
}
run "reject_escaping_prefix" {
  command = plan
  variables { trace_prefix = "../other" }
  expect_failures = [var.trace_prefix]
}
