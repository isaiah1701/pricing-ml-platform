variable "cluster_name" {
  description = "Name of the EKS cluster."
  type        = string
}

variable "shared_node_pool" {
  description = "Use only the system pool for a small shared learning deployment."
  type        = bool
  default     = false
}

variable "kubernetes_version" {
  description = "EKS Kubernetes minor version."
  type        = string
}

variable "vpc_id" {
  description = "VPC ID in which to create the cluster."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for the EKS control plane and managed node groups."
  type        = list(string)
}

variable "load_balancer_security_group_id" {
  description = "Security group allowed to reach instance-mode Kubernetes targets."
  type        = string
}

variable "cluster_endpoint_public_access" {
  description = "Whether the Kubernetes API has a public endpoint."
  type        = bool
  default     = false
}

variable "cluster_endpoint_public_access_cidrs" {
  description = "CIDRs permitted to access the public Kubernetes API endpoint when enabled."
  type        = list(string)
  default     = []
}

variable "system_node_pool" {
  description = "Always-on node pool for EKS and platform add-ons."
  type = object({
    instance_types = list(string)
    min_size       = number
    max_size       = number
    desired_size   = number
    disk_size      = optional(number, 50)
    capacity_type  = optional(string, "ON_DEMAND")
  })
}

variable "serving_node_pool" {
  description = "Dedicated node pool for online model-serving workloads."
  type = object({
    instance_types = list(string)
    min_size       = number
    max_size       = number
    desired_size   = number
    disk_size      = optional(number, 50)
    capacity_type  = optional(string, "ON_DEMAND")
  })
}

variable "training_node_pool" {
  description = "Dedicated node pool for offline CPU training jobs. It intentionally has no GPU configuration."
  type = object({
    instance_types = list(string)
    min_size       = number
    max_size       = number
    desired_size   = number
    disk_size      = optional(number, 100)
    capacity_type  = optional(string, "ON_DEMAND")
  })
}

variable "access_entries" {
  description = "Explicit EKS API access entries for CI and platform operators."
  type = map(object({
    principal_arn     = string
    kubernetes_groups = optional(list(string))
    type              = optional(string, "STANDARD")
    user_name         = optional(string)
    tags              = optional(map(string), {})
    policy_associations = optional(map(object({
      policy_arn = string
      access_scope = object({
        namespaces = optional(list(string))
        type       = string
      })
    })), {})
  }))
  default = {}
}

variable "bootstrap_cluster_creator_admin" {
  description = "Grant the Terraform caller initial cluster-admin access when no stable administrator ARN is configured."
  type        = bool
  default     = false
}

variable "kms_key_administrators" {
  description = "IAM principal ARNs permitted to administer the EKS encryption key."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to EKS resources."
  type        = map(string)
  default     = {}
}
