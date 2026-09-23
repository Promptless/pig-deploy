terraform {
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
  required_version = ">= 1.11.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.65.0"
    }
  }
}
