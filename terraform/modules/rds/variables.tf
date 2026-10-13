variable "database_identifier" {
  description = "RDS instance identifier. Defaults to ml-db, the AWS-compatible identifier for ML-DB."
  type        = string
  default     = "ml-db"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,62}$", var.database_identifier)) && !endswith(var.database_identifier, "-") && !strcontains(var.database_identifier, "--")
    error_message = "database_identifier must start with a lowercase letter, contain only lowercase letters, numbers, and single hyphens, and not end with a hyphen."
  }
}

variable "database_name" {
  description = "Initial PostgreSQL database name. Defaults to mldb, the PostgreSQL-compatible name for ML-DB."
  type        = string
  default     = "mldb"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9]{0,62}$", var.database_name))
    error_message = "database_name must start with a letter and contain only letters and numbers."
  }
}

variable "username" {
  description = "Master username for the PostgreSQL instance."
  type        = string

  validation {
    condition     = length(trimspace(var.username)) > 0
    error_message = "username must not be empty."
  }
}

variable "password" {
  description = "Master password when AWS-managed credentials are disabled."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true

  validation {
    condition     = var.password == null || (length(var.password) >= 8 && length(var.password) <= 128 && !can(regex("[/\"@]", var.password)))
    error_message = "password must be 8-128 characters and cannot contain slash, double quote, or at-sign characters."
  }
}

variable "manage_master_user_password" {
  description = "Let RDS generate and rotate the master password in Secrets Manager."
  type        = bool
  default     = true
}

variable "performance_insights_enabled" {
  description = "Enable RDS Performance Insights."
  type        = bool
  default     = true
}

variable "postgresql_engine_version" {
  description = "PostgreSQL engine version. Set to null to use the AWS default available version."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.postgresql_engine_version == null || length(trimspace(var.postgresql_engine_version)) > 0
    error_message = "postgresql_engine_version must not be an empty string when set."
  }
}

variable "instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.medium"

  validation {
    condition     = can(regex("^db\\.", var.instance_class))
    error_message = "instance_class must be a valid RDS instance class beginning with db."
  }
}

variable "allocated_storage" {
  description = "Allocated storage in GiB."
  type        = number
  default     = 20

  validation {
    condition     = var.allocated_storage >= 20
    error_message = "allocated_storage must be at least 20 GiB for PostgreSQL."
  }
}

variable "max_allocated_storage" {
  description = "Maximum storage in GiB for storage autoscaling. Set to null to disable autoscaling."
  type        = number
  default     = 100
  nullable    = true

  validation {
    condition     = var.max_allocated_storage == null || var.max_allocated_storage >= 20
    error_message = "max_allocated_storage must be at least 20 GiB when set."
  }
}

variable "subnet_ids" {
  description = "Private subnet IDs used to create the DB subnet group."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2 && length(distinct(var.subnet_ids)) == length(var.subnet_ids)
    error_message = "Provide at least two distinct private subnet IDs."
  }
}

variable "security_group_ids" {
  description = "Security group IDs assigned to the RDS instance."
  type        = list(string)

  validation {
    condition     = length(var.security_group_ids) > 0
    error_message = "Provide at least one security group ID."
  }
}

variable "backup_retention_period" {
  description = "Number of days to retain automated backups."
  type        = number
  default     = 7

  validation {
    condition     = var.backup_retention_period >= 0 && var.backup_retention_period <= 35
    error_message = "backup_retention_period must be between 0 and 35 days."
  }
}

variable "multi_az" {
  description = "Enable Multi-AZ deployment for high availability."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Prevent accidental deletion of the RDS instance."
  type        = bool
  default     = false
}

variable "skip_final_snapshot" {
  description = "Skip a final DB snapshot when the instance is destroyed."
  type        = bool
  default     = true
}

variable "final_snapshot_identifier" {
  description = "Unique final snapshot identifier required when skip_final_snapshot is false."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.final_snapshot_identifier == null || can(regex("^[a-z][a-z0-9-]{0,254}$", var.final_snapshot_identifier))
    error_message = "final_snapshot_identifier must start with a lowercase letter and contain only lowercase letters, numbers, and hyphens."
  }
}

variable "tags" {
  description = "Tags applied to the RDS instance and DB subnet group."
  type        = map(string)
  default     = {}
}
