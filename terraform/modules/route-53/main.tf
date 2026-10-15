data "aws_route53_zone" "this" {
  count   = var.create_hosted_zone ? 0 : 1
  zone_id = var.hosted_zone_id
}

resource "aws_route53_zone" "this" {
  count = var.create_hosted_zone ? 1 : 0
  name  = var.hosted_zone_name
  tags  = var.tags
}

locals {
  zone_id = var.create_hosted_zone ? aws_route53_zone.this[0].zone_id : data.aws_route53_zone.this[0].zone_id
  zone_name = trimsuffix(
    var.create_hosted_zone ? aws_route53_zone.this[0].name : data.aws_route53_zone.this[0].name,
    ".",
  )
}

resource "aws_route53_record" "standard" {
  for_each = var.records

  zone_id         = local.zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = each.value.ttl
  records         = each.value.records
  allow_overwrite = each.value.allow_overwrite
}

resource "aws_route53_record" "alias" {
  for_each = var.alias_records

  zone_id = local.zone_id
  name    = each.value.name
  type    = each.value.type

  alias {
    name                   = each.value.target_dns_name
    zone_id                = each.value.target_zone_id
    evaluate_target_health = each.value.evaluate_target_health
  }
}
