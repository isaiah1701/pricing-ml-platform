output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS API endpoint."
  value       = module.eks.cluster_endpoint
}

output "ecr_repository_urls" {
  description = "Serving and training ECR repository URLs."
  value       = { for name, repository in module.ecr : name => repository.repository_url }
}

output "route53_name_servers" {
  description = "Registrar delegation targets when Terraform creates the hosted zone."
  value       = module.route53.hosted_zone_name_servers
}

output "application_hostname" {
  description = "Public hostname managed by ExternalDNS for the Traefik NLB."
  value       = var.application_hostname
}

output "route53_hosted_zone_id" {
  description = "Route 53 zone that will hold serving DNS aliases."
  value       = module.route53.hosted_zone_id
}

output "node_pool_scheduling" {
  description = "Node selectors and tolerations required by serving and training workloads."
  value       = module.eks.node_pool_scheduling
}

output "platform_data_bucket" {
  description = "Versioned and Object-Locked S3 bucket for datasets, snapshots, reports, and MLflow artifacts."
  value       = module.platform_data.bucket_name
}

output "mlflow_database_secret_arn" {
  description = "RDS-managed credential secret consumed through External Secrets Operator."
  value       = module.mlflow_database.master_user_secret_arn
}

output "airflow_database_secret_arn" {
  description = "RDS-managed credential secret consumed through External Secrets Operator."
  value       = module.airflow_database.master_user_secret_arn
}

output "redis_configuration_endpoint" {
  description = "TLS Redis configuration endpoint used by Feast."
  value       = module.redis.configuration_endpoint_address
}

output "pod_identity_role_arns" {
  description = "Least-privilege EKS Pod Identity roles for platform workloads and controllers."
  value       = { for name, role in aws_iam_role.pod_identity : name => role.arn }
}

output "deployment_coordinates" {
  description = "Non-secret values used to replace production Helm placeholders before Argo CD bootstrap."
  value = {
    account_id                  = data.aws_caller_identity.current.account_id
    aws_region                  = var.aws_region
    airflow_database_secret_arn = module.airflow_database.master_user_secret_arn
    airflow_database_host       = module.airflow_database.address
    airflow_database_port       = tostring(module.airflow_database.port)
    airflow_database_name       = module.airflow_database.database_name
    application_hostname        = var.application_hostname
    cluster_name                = module.eks.cluster_name
    domain_name                 = var.domain_name
    mlflow_artifact_root        = "s3://${module.platform_data.bucket_name}/${trimsuffix(var.mlflow_artifact_prefix, "/")}"
    mlflow_database_secret_arn  = module.mlflow_database.master_user_secret_arn
    mlflow_database_host        = module.mlflow_database.address
    mlflow_database_port        = tostring(module.mlflow_database.port)
    mlflow_database_name        = module.mlflow_database.database_name
    platform_data_bucket        = module.platform_data.bucket_name
    redis_connection_string     = "${module.redis.configuration_endpoint_address}:${module.redis.port},ssl=true"
    serving_repository          = module.ecr["serving"].repository_url
    training_repository         = module.ecr["training"].repository_url
  }
}
