variable "bucket_name" {
  description = "Globally unique name for the private MLflow artifact bucket."
  type        = string

  validation {
    condition     = length(var.bucket_name) >= 3 && length(var.bucket_name) <= 63
    error_message = "bucket_name must be between 3 and 63 characters long."
  }
}

variable "noncurrent_version_expiration_days" {
  description = "Number of days to retain noncurrent object versions. Set to null to disable expiration."
  type        = number
  default     = 90
  nullable    = true

  validation {
    condition     = var.noncurrent_version_expiration_days == null ? true : var.noncurrent_version_expiration_days >= 1
    error_message = "noncurrent_version_expiration_days must be at least 1 when set."
  }
}

variable "object_lock_enabled" {
  description = "Enable S3 Object Lock for immutable model, data, and lineage artifacts."
  type        = bool
  default     = false
}

variable "object_lock_retention_days" {
  description = "Default governance retention period when Object Lock is enabled."
  type        = number
  default     = 90

  validation {
    condition     = var.object_lock_retention_days >= 1
    error_message = "object_lock_retention_days must be at least one day."
  }
}

variable "tags" {
  description = "Tags applied to the S3 bucket."
  type        = map(string)
  default     = {}
}
