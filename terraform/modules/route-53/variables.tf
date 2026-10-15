variable "hosted_zone_id" {
  description = "Existing Route 53 public hosted-zone ID. Leave null when this module creates the zone."
  type        = string
  default     = null
  nullable    = true
}

variable "create_hosted_zone" {
  description = "Create the public hosted zone instead of reading an existing zone ID."
  type        = bool
  default     = false
}

variable "hosted_zone_name" {
  description = "Public DNS zone to create when create_hosted_zone is true."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.create_hosted_zone ? try(length(trimspace(var.hosted_zone_name)) > 0, false) : true
    error_message = "hosted_zone_name is required when create_hosted_zone is true."
  }
}

variable "records" {
  description = "Standard Route 53 records keyed by a stable Terraform identifier."
  type = map(object({
    name            = string
    type            = string
    ttl             = number
    records         = list(string)
    allow_overwrite = optional(bool, false)
  }))
  default = {}
}

variable "alias_records" {
  description = "Alias records keyed by a stable Terraform identifier. Use this for an ALB once serving ingress exists."
  type = map(object({
    name                   = string
    type                   = string
    target_dns_name        = string
    target_zone_id         = string
    evaluate_target_health = optional(bool, true)
  }))
  default = {}
}

variable "tags" {
  description = "Tags applied to the hosted zone. Route 53 records do not support tags."
  type        = map(string)
  default     = {}
}
