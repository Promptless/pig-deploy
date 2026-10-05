data "aws_eks_cluster" "existing" { name = var.eks_cluster_name }
locals {
  issuer = replace(data.aws_eks_cluster.existing.identity[0].oidc[0].issuer, "https://", "")
  prefix = trim(var.trace_prefix, "/")
}
resource "aws_s3_bucket" "traces" {
  bucket        = var.trace_bucket_name
  force_destroy = false
  tags          = var.tags
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "traces" {
  bucket                  = aws_s3_bucket.traces.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "traces" {
  bucket = aws_s3_bucket.traces.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "traces" {
  bucket = aws_s3_bucket.traces.id
  rule {
    bucket_key_enabled = var.kms_key_arn != null
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
  }
}
resource "aws_s3_bucket_policy" "traces" {
  bucket = aws_s3_bucket.traces.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [aws_s3_bucket.traces.arn, "${aws_s3_bucket.traces.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_s3_bucket_lifecycle_configuration" "traces" {
  bucket     = aws_s3_bucket.traces.id
  depends_on = [aws_s3_bucket_versioning.traces]
  rule {
    id     = "trace-recovery"
    status = "Enabled"
    filter { prefix = "${local.prefix}/" }
    noncurrent_version_expiration { noncurrent_days = var.object_recovery_days }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
    dynamic "expiration" {
      for_each = var.trace_retention_days > 0 ? [var.trace_retention_days] : []
      content { days = expiration.value }
    }
  }
}
resource "aws_iam_role" "analyzer" {
  name = "${var.name}-analyzer"
  tags = var.tags
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = var.oidc_provider_arn },
    Condition = { StringEquals = {
      "${local.issuer}:sub" = "system:serviceaccount:${var.namespace}:${var.service_account_name}",
      "${local.issuer}:aud" = "sts.amazonaws.com"
    } }
  }] })
  lifecycle {
    precondition {
      condition     = endswith(var.oidc_provider_arn, ":oidc-provider/${local.issuer}")
      error_message = "The IAM OIDC provider must match the existing EKS cluster issuer."
    }
  }
}
resource "aws_iam_role_policy" "traces" {
  name = "native-trace-objects"
  role = aws_iam_role.analyzer.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([{
    Effect   = "Allow", Action = ["s3:GetObject", "s3:PutObject"],
    Resource = "${aws_s3_bucket.traces.arn}/${local.prefix}/*"
    }], var.kms_key_arn == null ? [] : [{
    Effect    = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = var.kms_key_arn,
    Condition = { StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" } }
  }]) })
}
resource "aws_db_subnet_group" "pig" {
  name       = var.name
  subnet_ids = var.database_subnet_ids
  tags       = var.tags
}
resource "aws_security_group" "postgres" {
  name_prefix = "${var.name}-postgres-"
  description = "PIG PostgreSQL traffic from existing analyzer workloads"
  vpc_id      = var.vpc_id
  tags        = var.tags
}
resource "aws_vpc_security_group_ingress_rule" "postgres" {
  security_group_id            = aws_security_group.postgres.id
  referenced_security_group_id = var.workload_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}
resource "aws_db_parameter_group" "postgres" {
  name_prefix = "${var.name}-"
  family      = "postgres${var.postgres_version}"
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }
  tags = var.tags
  lifecycle { create_before_destroy = true }
}
resource "aws_db_instance" "postgres" {
  identifier                  = var.name
  engine                      = "postgres"
  engine_version              = var.postgres_version
  instance_class              = var.postgres_instance_class
  db_name                     = var.postgres_database
  username                    = var.postgres_user
  password                    = var.postgres_password
  allocated_storage           = var.postgres_storage_gib
  max_allocated_storage       = var.postgres_max_storage_gib
  storage_type                = "gp3"
  storage_encrypted           = true
  kms_key_id                  = var.kms_key_arn
  multi_az                    = var.postgres_multi_az
  publicly_accessible         = false
  db_subnet_group_name        = aws_db_subnet_group.pig.name
  vpc_security_group_ids      = [aws_security_group.postgres.id]
  parameter_group_name        = aws_db_parameter_group.postgres.name
  backup_retention_period     = var.backup_retention_days
  deletion_protection         = var.deletion_protection
  skip_final_snapshot         = false
  final_snapshot_identifier   = "${var.name}-final"
  copy_tags_to_snapshot       = true
  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false
  apply_immediately           = false
  tags                        = var.tags
  lifecycle { prevent_destroy = true }
}
