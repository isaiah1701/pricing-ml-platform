resource "aws_db_subnet_group" "this" {
  name       = "${var.database_identifier}-subnet-group"
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

resource "aws_db_instance" "this" {
  identifier = var.database_identifier

  engine         = "postgres"
  engine_version = var.postgresql_engine_version
  instance_class = var.instance_class

  db_name                     = var.database_name
  username                    = var.username
  password                    = var.manage_master_user_password ? null : var.password
  manage_master_user_password = var.manage_master_user_password

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = var.security_group_ids
  publicly_accessible    = false

  backup_retention_period         = var.backup_retention_period
  multi_az                        = var.multi_az
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = var.skip_final_snapshot
  final_snapshot_identifier       = var.skip_final_snapshot ? null : var.final_snapshot_identifier
  copy_tags_to_snapshot           = true
  performance_insights_enabled    = var.performance_insights_enabled
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  tags = var.tags

  lifecycle {
    precondition {
      condition     = var.max_allocated_storage == null || var.max_allocated_storage >= var.allocated_storage
      error_message = "max_allocated_storage must be greater than or equal to allocated_storage when set."
    }

    precondition {
      condition     = var.skip_final_snapshot || var.final_snapshot_identifier != null
      error_message = "final_snapshot_identifier must be set when skip_final_snapshot is false."
    }

    precondition {
      condition     = var.manage_master_user_password || var.password != null
      error_message = "password is required when manage_master_user_password is false."
    }
  }
}
