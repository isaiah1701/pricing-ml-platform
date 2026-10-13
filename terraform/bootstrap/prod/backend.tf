terraform {
  # Bootstrap uses local state because it creates the S3 bucket used by the
  # corresponding environment's remote backend.
  backend "local" {}

  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.1"
    }
  }
}
