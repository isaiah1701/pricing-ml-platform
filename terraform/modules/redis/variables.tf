variable "replication_group_id" {
  description = "Unique identifier for the Redis replication group."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,39}$", var.replication_group_id)) && !endswith(var.replication_group_id, "-") && !strcontains(var.replication_group_id, "--")
    error_message = "replication_group_id must start with a lowercase letter, contain only lowercase letters, numbers, and single hyphens, and be at most 40 characters."
  }
}

variable "vpc_id" {
  description = "ID of the VPC in which to create the Redis security group."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]+$", var.vpc_id))
    error_message = "vpc_id must be a valid AWS VPC ID."
  }
}

variable "subnet_ids" {
  description = "Private subnet IDs used by Redis; provide subnets in at least two Availability Zones."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2 && length(distinct(var.subnet_ids)) == length(var.subnet_ids)
    error_message = "Provide at least two distinct private subnet IDs for Multi-AZ deployment."
  }
}

variable "allowed_cidr_blocks" {
  description = "IPv4 CIDR blocks allowed to connect to Redis."
  type        = list(string)
  default     = []
}

variable "allowed_security_group_ids" {
  description = "Security group IDs allowed to connect to Redis."
  type        = list(string)
  default     = []
}

variable "engine_version" {
  description = "Redis engine version."
  type        = string
  default     = "7.1"
}

variable "node_type" {
  description = "ElastiCache node type. The default is a low-cost Graviton instance."
  type        = string
  default     = "cache.t4g.micro"

  validation {
    condition     = startswith(var.node_type, "cache.")
    error_message = "node_type must be a valid ElastiCache type beginning with cache."
  }
}

variable "port" {
  description = "Port on which Redis accepts connections."
  type        = number
  default     = 6379

  validation {
    condition     = var.port >= 1 && var.port <= 65535
    error_message = "port must be between 1 and 65535."
  }
}

variable "num_node_groups" {
  description = "Number of Redis shards. One shard minimizes cost."
  type        = number
  default     = 1

  validation {
    condition     = var.num_node_groups >= 1
    error_message = "num_node_groups must be at least 1."
  }
}

variable "replicas_per_node_group" {
  description = "Number of replicas per shard. Multi-AZ failover requires at least one."
  type        = number
  default     = 1

  validation {
    condition     = var.replicas_per_node_group >= 0 && var.replicas_per_node_group <= 5
    error_message = "replicas_per_node_group must be between 0 and 5; zero disables HA for learning deployments."
  }
}

variable "transit_encryption_enabled" {
  description = "Encrypt Redis traffic in transit."
  type        = bool
  default     = true
}

variable "maintenance_window" {
  description = "Weekly UTC maintenance window."
  type        = string
  default     = "sun:05:00-sun:06:00"
}

variable "apply_immediately" {
  description = "Apply changes immediately instead of during the maintenance window."
  type        = bool
  default     = false
}

variable "parameter_group_family" {
  description = "Redis parameter group family compatible with the selected engine version."
  type        = string
  default     = "redis7"
}

variable "parameters" {
  description = "Redis parameter group settings."
  type        = list(map(string))
  default = [
    {
      name  = "latency-tracking"
      value = "yes"
    }
  ]
}

variable "tags" {
  description = "Additional tags applied to all ElastiCache resources."
  type        = map(string)
  default     = {}
}
