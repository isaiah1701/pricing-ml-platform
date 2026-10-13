locals {
  resource_tags = merge({
    Terraform = "true"
  }, var.tags)
}

resource "aws_elasticache_subnet_group" "this" {
  name        = "${var.replication_group_id}-subnets"
  description = "Private subnets for ${var.replication_group_id}"
  subnet_ids  = var.subnet_ids

  tags = local.resource_tags
}

resource "aws_elasticache_parameter_group" "this" {
  name        = "${var.replication_group_id}-parameters"
  description = "Redis parameters for ${var.replication_group_id}"
  family      = var.parameter_group_family

  dynamic "parameter" {
    for_each = var.parameters

    content {
      name  = parameter.value.name
      value = parameter.value.value
    }
  }

  tags = local.resource_tags
}

resource "aws_security_group" "this" {
  name_prefix = "${var.replication_group_id}-"
  description = "Controls access to the ${var.replication_group_id} Redis cluster"
  vpc_id      = var.vpc_id

  tags = merge(local.resource_tags, {
    Name = "${var.replication_group_id}-redis"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "cidr" {
  for_each = toset(var.allowed_cidr_blocks)

  security_group_id = aws_security_group.this.id
  description       = "Redis access from ${each.value}"
  ip_protocol       = "tcp"
  from_port         = var.port
  to_port           = var.port
  cidr_ipv4         = each.value

  tags = local.resource_tags
}

resource "aws_vpc_security_group_ingress_rule" "security_group" {
  count = length(var.allowed_security_group_ids)

  security_group_id            = aws_security_group.this.id
  description                  = "Redis access from security group ${count.index + 1}"
  ip_protocol                  = "tcp"
  from_port                    = var.port
  to_port                      = var.port
  referenced_security_group_id = var.allowed_security_group_ids[count.index]

  tags = local.resource_tags
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = var.replication_group_id
  description          = "Multi-AZ Redis cluster for ${var.replication_group_id}"

  engine         = "redis"
  engine_version = var.engine_version
  node_type      = var.node_type
  port           = var.port

  # A single shard with one replica is the cheapest cluster-mode topology that
  # provides automatic failover in another Availability Zone.
  cluster_mode               = "enabled"
  num_node_groups            = var.num_node_groups
  replicas_per_node_group    = var.replicas_per_node_group
  automatic_failover_enabled = true
  multi_az_enabled           = true

  at_rest_encryption_enabled = true
  transit_encryption_enabled = var.transit_encryption_enabled

  subnet_group_name    = aws_elasticache_subnet_group.this.name
  parameter_group_name = aws_elasticache_parameter_group.this.name
  security_group_ids   = [aws_security_group.this.id]

  maintenance_window = var.maintenance_window
  apply_immediately  = var.apply_immediately

  tags = local.resource_tags
}
