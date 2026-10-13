output "cluster_arn" {
  description = "ARN of the EKS cluster."
  value       = module.eks.cluster_arn
}

output "cluster_certificate_authority_data" {
  description = "Base64-encoded cluster CA data for Kubernetes clients."
  value       = module.eks.cluster_certificate_authority_data
  sensitive   = true
}

output "cluster_endpoint" {
  description = "EKS Kubernetes API endpoint."
  value       = module.eks.cluster_endpoint
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "node_security_group_id" {
  description = "Security group shared by EKS managed node groups."
  value       = module.eks.node_security_group_id
}

output "cluster_oidc_provider_arn" {
  description = "OIDC provider ARN for workload IAM roles."
  value       = module.eks.oidc_provider_arn
}

output "node_pool_scheduling" {
  description = "Node labels and taints that workloads must use to target dedicated pools."
  value = {
    serving = {
      node_selector = { workload = "serving" }
      toleration    = { key = "workload", operator = "Equal", value = "serving", effect = "NoSchedule" }
    }
    training = {
      node_selector = { workload = "training" }
      toleration    = { key = "workload", operator = "Equal", value = "training", effect = "NoSchedule" }
    }
  }
}
