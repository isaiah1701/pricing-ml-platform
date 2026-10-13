provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "tls_certificate" "github" {
  count = var.github_repository == null ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

locals {
  name_prefix       = "${var.project_name}-${var.environment}"
  state_bucket_name = lower("${local.name_prefix}-terraform-state-${data.aws_caller_identity.current.account_id}")
  state_key         = "${var.project_name}/${var.environment}/terraform.tfstate"

  common_tags = merge({
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "Terraform"
    System      = "mlops"
    Stack       = "bootstrap"
  }, var.tags)
}
