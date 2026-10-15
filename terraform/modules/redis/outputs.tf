output "replication_group_id" {
  description = "ID of the Redis replication group."
  value       = aws_elasticache_replication_group.this.id
}

output "replication_group_arn" {
  description = "ARN of the Redis replication group."
  value       = aws_elasticache_replication_group.this.arn
}

output "configuration_endpoint_address" {
  description = "Cluster configuration endpoint or the budget standalone primary endpoint."
  value       = var.replicas_per_node_group > 0 ? aws_elasticache_replication_group.this.configuration_endpoint_address : aws_elasticache_replication_group.this.primary_endpoint_address
}

output "port" {
  description = "Port on which Redis accepts connections."
  value       = var.port
}

output "security_group_id" {
  description = "ID of the security group created for Redis."
  value       = aws_security_group.this.id
}

output "subnet_group_name" {
  description = "Name of the ElastiCache subnet group."
  value       = aws_elasticache_subnet_group.this.name
}

output "parameter_group_id" {
  description = "Name of the Redis parameter group."
  value       = aws_elasticache_parameter_group.this.id
}
