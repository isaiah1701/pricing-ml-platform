variable "aws_region" {
  description = "AWS region for this environment."
  type        = string
  default     = "eu-west-2"
}

variable "cost_optimized" {
  description = "Use the single-node learning topology only when explicitly requested; production autoscaling requires the dedicated node pools."
  type        = bool
  default     = false
}

variable "project_name" {
  description = "Short project identifier used in resource names."
  type        = string
  default     = "prod-mlops"
}

variable "domain_name" {
  description = "Production DNS zone used by the public model-serving endpoint."
  type        = string
  default     = "platf0rm.site"
}

variable "application_hostname" {
  description = "Public DNS hostname for the Traefik inference endpoint."
  type        = string
  default     = "platf0rm.site"
}

variable "alert_notification_email" {
  description = "Optional email endpoint subscribed to production Alertmanager notifications through SNS. AWS requires confirmation."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.alert_notification_email == null || can(regex("^[^@]+@[^@]+\\.[^@]+$", var.alert_notification_email))
    error_message = "alert_notification_email must be a valid email address when set."
  }
}

variable "create_route53_hosted_zone" {
  description = "Create the public hosted zone in this account. Set false when supplying an existing zone ID."
  type        = bool
  default     = true
}

variable "route53_hosted_zone_id" {
  description = "Existing Route 53 hosted-zone ID when create_route53_hosted_zone is false."
  type        = string
  default     = null
  nullable    = true
}

variable "platform_admin_principal_arn" {
  description = "Optional stable IAM administrator for EKS and its KMS key. When omitted, the Terraform caller is the initial cluster administrator."
  type        = string
  default     = null
  nullable    = true
}

variable "vpc_cidr" {
  description = "CIDR block for the environment VPC."
  type        = string
  default     = "10.22.0.0/20"
}

variable "azs" {
  description = "Availability zones for the VPC."
  type        = list(string)
  default     = ["eu-west-2a", "eu-west-2b"]
}

variable "public_subnet_cidrs" {
  description = "Public subnet CIDRs."
  type        = list(string)
  default     = ["10.22.0.0/24", "10.22.1.0/24"]
}

variable "private_subnet_cidrs" {
  description = "Private subnet CIDRs."
  type        = list(string)
  default     = ["10.22.8.0/24", "10.22.9.0/24"]
}

variable "interface_endpoint_services" {
  description = "Private AWS services exposed to workloads through interface endpoints."
  type        = list(string)
  default     = ["ecr.api", "ecr.dkr", "logs", "secretsmanager", "sts"]
}

variable "kubernetes_version" {
  description = "EKS Kubernetes minor version."
  type        = string
  default     = "1.35"
}

variable "cluster_endpoint_public_access" {
  description = "Expose a public EKS API endpoint."
  type        = bool
  default     = false
}

variable "cluster_endpoint_public_access_cidrs" {
  description = "CIDRs allowed to access the public EKS API endpoint when enabled."
  type        = list(string)
  default     = []
}

variable "system_node_pool" {
  description = "Capacity for cluster add-ons and system components."
  type = object({
    instance_types = list(string)
    min_size       = number
    max_size       = number
    desired_size   = number
    disk_size      = optional(number, 50)
    capacity_type  = optional(string, "ON_DEMAND")
  })
  default = {
    instance_types = ["m7i.large"]
    min_size       = 2
    max_size       = 4
    desired_size   = 2
  }
}

variable "serving_node_pool" {
  description = "Dedicated online-serving capacity."
  type = object({
    instance_types = list(string)
    min_size       = number
    max_size       = number
    desired_size   = number
    disk_size      = optional(number, 50)
    capacity_type  = optional(string, "ON_DEMAND")
  })
  default = {
    instance_types = ["m7i.large"]
    min_size       = 2
    max_size       = 8
    desired_size   = 2
  }
}

variable "training_node_pool" {
  description = "Dedicated CPU capacity for Airflow LocalExecutor and training; keep one on-demand node for the control plane."
  type = object({
    instance_types = list(string)
    min_size       = number
    max_size       = number
    desired_size   = number
    disk_size      = optional(number, 100)
    capacity_type  = optional(string, "ON_DEMAND")
  })
  default = {
    instance_types = ["m7i.xlarge"]
    min_size       = 1
    max_size       = 5
    desired_size   = 1
    capacity_type  = "ON_DEMAND"
  }
}

variable "access_entries" {
  description = "Additional explicit EKS API access entries for CI or operators."
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

variable "kms_key_administrators" {
  description = "Additional IAM principal ARNs allowed to administer the EKS KMS key."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default = {
    Owner = "isaiah1701"
  }
}

variable "external_secrets_namespace" {
  description = "Namespace containing the External Secrets Operator controller."
  type        = string
  default     = "external-secrets"
}

variable "aws_load_balancer_controller_service_account" {
  description = "Service account used by AWS Load Balancer Controller."
  type        = string
  default     = "aws-load-balancer-controller"
}

variable "cluster_autoscaler_service_account" {
  description = "Service account used by Cluster Autoscaler."
  type        = string
  default     = "cluster-autoscaler"
}

variable "external_dns_service_account" {
  description = "Service account used by ExternalDNS."
  type        = string
  default     = "external-dns"
}

variable "external_secrets_service_account" {
  description = "ESO service account bound through EKS Pod Identity."
  type        = string
  default     = "external-secrets"
}

variable "inference_namespace" {
  description = "Namespace containing the model-serving workload."
  type        = string
  default     = "airbnb-inference"
}

variable "inference_service_account" {
  description = "Service account used by the model-serving workload."
  type        = string
  default     = "airbnb-inference"
}

variable "mlflow_namespace" {
  description = "Namespace containing the MLflow tracking server."
  type        = string
  default     = "mlflow"
}

variable "mlflow_service_account" {
  description = "Service account used by the MLflow tracking server."
  type        = string
  default     = "mlflow"
}

variable "airflow_namespace" {
  description = "Namespace containing the Airflow control plane and training jobs."
  type        = string
  default     = "airflow"
}

variable "airflow_service_account" {
  description = "Service account used by Airflow and its training tasks."
  type        = string
  default     = "airflow"
}

variable "artifact_retention_days" {
  description = "Governance-mode S3 Object Lock retention for data and model artifacts."
  type        = number
  default     = 90

  validation {
    condition     = var.artifact_retention_days >= 1
    error_message = "artifact_retention_days must be at least one day."
  }
}

variable "database_instance_class" {
  description = "RDS instance class used independently by MLflow and Airflow."
  type        = string
  default     = "db.t4g.medium"
}

variable "database_backup_retention_days" {
  description = "Automated RDS backup retention period."
  type        = number
  default     = 14

  validation {
    condition     = var.database_backup_retention_days >= 7 && var.database_backup_retention_days <= 35
    error_message = "database_backup_retention_days must be between 7 and 35."
  }
}

variable "redis_node_type" {
  description = "ElastiCache node type for the Feast online store."
  type        = string
  default     = "cache.t4g.small"
}

variable "grafana_smtp_secret_arn" {
  description = "Optional existing eu-west-2 SMTP secret read by the existing External Secrets role; never an SMTP credential."
  type        = string
  default     = ""

  validation {
    condition     = var.grafana_smtp_secret_arn == "" || can(regex("^arn:aws:secretsmanager:eu-west-2:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$", var.grafana_smtp_secret_arn))
    error_message = "Use an existing eu-west-2 Secrets Manager secret ARN or leave SMTP disabled."
  }
}

variable "mlflow_artifact_prefix" {
  description = "S3 key prefix that inference may read when artifact storage uses S3."
  type        = string
  default     = "mlflow"

  validation {
    condition     = length(trim(var.mlflow_artifact_prefix, "/")) > 0
    error_message = "mlflow_artifact_prefix must contain a non-slash prefix."
  }
}
