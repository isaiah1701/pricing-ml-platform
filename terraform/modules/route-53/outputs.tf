output "hosted_zone_id" {
  description = "Route 53 hosted-zone ID."
  value       = local.zone_id
}

output "hosted_zone_name_servers" {
  description = "Authoritative name servers when this module creates the hosted zone."
  value       = try(aws_route53_zone.this[0].name_servers, [])
}

output "hosted_zone_name" {
  description = "Route 53 hosted-zone name without a trailing dot."
  value       = local.zone_name
}

output "certificate_arn" {
  description = "Validated ACM certificate ARN, or null when certificate creation is disabled."
  value       = try(aws_acm_certificate_validation.this[0].certificate_arn, null)
}
