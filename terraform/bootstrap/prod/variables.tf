variable "aws_region" {
  description = "AWS region shared by this bootstrap state bucket and environment."
  type        = string
  default     = "eu-west-2"
}

variable "project_name" {
  description = "Short project identifier used in bootstrap resource names."
  type        = string
  default     = "prod-mlops"
}

variable "environment" {
  description = "Environment name used in bootstrap resource names."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging, or prod."
  }
}

variable "terraform_trusted_principal_arns" {
  description = "IAM user or role ARNs allowed to assume the Terraform execution role."
  type        = list(string)

  validation {
    condition     = length(var.terraform_trusted_principal_arns) > 0 && alltrue([for principal_arn in var.terraform_trusted_principal_arns : can(regex("^arn:", principal_arn))])
    error_message = "Provide at least one valid IAM user or role ARN."
  }
}

variable "github_repository" {
  description = "Optional OWNER/REPOSITORY allowed to assume the role through an existing GitHub Actions OIDC provider."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.github_repository == null || can(regex("^[^/]+/[^/]+$", var.github_repository))
    error_message = "github_repository must use the OWNER/REPOSITORY format when set."
  }
}

variable "github_environment" {
  description = "Protected GitHub Environment allowed to publish production images."
  type        = string
  default     = "production"

  validation {
    condition     = length(trimspace(var.github_environment)) > 0
    error_message = "github_environment must not be empty."
  }
}

variable "tags" {
  description = "Additional tags applied to bootstrap resources."
  type        = map(string)
  default = {
    Owner = "isaiah1701"
  }
}
