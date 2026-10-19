# Proposal renderer only: no providers or cloud/Kubernetes resources.
terraform {
  required_version = ">= 1.11.0"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Existing private subnets, selected from the platform Terraform outputs."
  validation {
    condition     = length(var.private_subnet_ids) > 0 && alltrue([for id in var.private_subnet_ids : can(regex("^subnet-[0-9a-f]+$", id))])
    error_message = "Provide existing private subnet IDs."
  }
}

variable "node_security_group_id" {
  type        = string
  description = "Existing EKS node security group; never the load-balancer group."
  validation {
    condition     = can(regex("^sg-[0-9a-f]+$", var.node_security_group_id))
    error_message = "Provide an existing node security group ID."
  }
}

variable "node_instance_profile" {
  type        = string
  description = "Pre-created, reviewed Karpenter worker instance profile (not provisioned here)."
  validation {
    condition     = length(trimspace(var.node_instance_profile)) > 0
    error_message = "A reviewed worker instance profile is required."
  }
}

variable "approved_ami_id" {
  type        = string
  description = "Pinned EKS-compatible AL2023 amd64 AMI in eu-west-2, validated before release."
  validation {
    condition     = can(regex("^ami-[0-9a-f]+$", var.approved_ami_id))
    error_message = "Use an explicitly approved AMI ID, never a moving latest alias."
  }
}

locals {
  # The fixed managed node group remains min=desired=max=1 in production.
  fixed_nodes = 1
  node_pool = {
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata   = { name = "inference-burst" }
    spec = {
      template = {
        metadata = { labels = { workload = "system" } }
        spec = {
          nodeClassRef = { group = "karpenter.k8s.aws", kind = "EC2NodeClass", name = "inference-burst" }
          # Only inference tolerates this taint. Airflow cannot spill onto this pool.
          taints      = [{ key = "workload", value = "serving", effect = "NoSchedule" }]
          expireAfter = "Never"
          requirements = [
            { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
            { key = "kubernetes.io/os", operator = "In", values = ["linux"] },
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["on-demand"] },
            { key = "node.kubernetes.io/instance-type", operator = "In", values = ["r5a.large"] },
          ]
        }
      }
      # Nominal capacity of ONE r5a.large. Eventually consistent, NOT a hard cap.
      limits = { cpu = "2", memory = "16Gi" }
      disruption = {
        consolidationPolicy = "WhenEmptyOrUnderutilized"
        consolidateAfter    = "5m"
        budgets             = [{ nodes = "1" }]
      }
    }
  }
  node_class = {
    apiVersion = "karpenter.k8s.aws/v1"
    kind       = "EC2NodeClass"
    metadata   = { name = "inference-burst" }
    spec = {
      amiFamily                  = "AL2023"
      amiSelectorTerms           = [{ id = var.approved_ami_id }]
      instanceProfile            = var.node_instance_profile
      subnetSelectorTerms        = [for id in var.private_subnet_ids : { id = id }]
      securityGroupSelectorTerms = [{ id = var.node_security_group_id }]
      metadataOptions            = { httpTokens = "required", httpPutResponseHopLimit = 2 }
      blockDeviceMappings = [{
        deviceName = "/dev/xvda"
        ebs        = { volumeSize = "20Gi", volumeType = "gp3", encrypted = true, deleteOnTermination = true }
      }]
      tags = { Environment = "prod", Project = "prod-mlops", Purpose = "inference-burst" }
    }
  }
}

output "node_pool" {
  description = "Candidate manifest only; strict two-node limit is not proven."
  value       = local.node_pool
}

output "node_class" {
  description = "Candidate NodeClass, with explicit pre-existing AWS coordinates."
  value       = local.node_class
}

output "budget" {
  description = "Design intent only, not an enforced global capacity guarantee."
  value       = { fixed_nodes = local.fixed_nodes, nominal_burst_nodes = 1, nominal_total = 2, safe_to_deploy = false }
}
