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
  description = "Backend configuration for the matching environment root."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.terraform_state.id}"
    key          = "${local.state_key}"
    region       = "${var.aws_region}"
    encrypt      = true
    use_lockfile = true
  EOT
}
