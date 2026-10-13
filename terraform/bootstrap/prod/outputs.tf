output "terraform_state_bucket_name" {
  description = "S3 bucket name for this environment's Terraform backend."
  value       = aws_s3_bucket.terraform_state.id
}

output "terraform_state_bucket_arn" {
  description = "ARN of this environment's Terraform state bucket."
  value       = aws_s3_bucket.terraform_state.arn
}

output "terraform_state_key" {
  description = "S3 object key for this environment's Terraform state."
  value       = local.state_key
}

output "terraform_execution_role_arn" {
  description = "Role ARN for protected Terraform plans and applies."
  value       = aws_iam_role.terraform_execution.arn
}

output "environment_backend_hcl" {
  description = "Backend configuration for the matching production environment root."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.terraform_state.id}"
    key          = "${local.state_key}"
    region       = "${var.aws_region}"
    encrypt      = true
    use_lockfile = true
  EOT
}

output "github_ecr_publish_role_arn" {
  description = "Role configured in GitHub as AWS_ECR_PUBLISH_ROLE_ARN."
  value       = try(aws_iam_role.github_ecr_publish[0].arn, null)
}

output "github_oidc_provider_arn" {
  description = "GitHub Actions OIDC provider created for short-lived CI credentials."
  value       = try(aws_iam_openid_connect_provider.github[0].arn, null)
}
