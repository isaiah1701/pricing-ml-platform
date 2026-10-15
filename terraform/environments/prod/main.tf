data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

check "route53_configuration" {
  assert {
    condition     = var.create_route53_hosted_zone != (var.route53_hosted_zone_id != null)
    error_message = "Create a hosted zone or supply an existing hosted-zone ID, but not both."
  }
}

check "application_hostname" {
  assert {
    condition     = var.application_hostname == var.domain_name || endswith(var.application_hostname, ".${var.domain_name}")
    error_message = "application_hostname must belong to domain_name."
  }
}

locals {
  environment = "prod"
  name        = "${var.project_name}-${local.environment}"

  common_tags = merge({
    Project     = var.project_name
    Environment = local.environment
    ManagedBy   = "Terraform"
    System      = "mlops"
  }, var.tags)

  ecr_repositories = toset(["serving", "training"])

  access_entries = merge(
    var.platform_admin_principal_arn == null ? {} : {
      platform_admin = {
        principal_arn = var.platform_admin_principal_arn
        policy_associations = {
          cluster_admin = {
            policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
            access_scope = {
              type = "cluster"
            }
          }
        }
      }
    },
    var.access_entries
  )

  pod_identities = {
    airflow = {
      namespace       = var.airflow_namespace
      service_account = var.airflow_service_account
    }
    aws_load_balancer_controller = {
      namespace       = "kube-system"
      service_account = var.aws_load_balancer_controller_service_account
    }
    cluster_autoscaler = {
      namespace       = "kube-system"
      service_account = var.cluster_autoscaler_service_account
    }
    external_dns = {
      namespace       = "kube-system"
      service_account = var.external_dns_service_account
    }
    external_secrets = {
      namespace       = var.external_secrets_namespace
      service_account = var.external_secrets_service_account
    }
    inference = {
      namespace       = var.inference_namespace
      service_account = var.inference_service_account
    }
    mlflow = {
      namespace       = var.mlflow_namespace
      service_account = var.mlflow_service_account
    }
  }
}

module "vpc" {
  source = "../../modules/vpc"

  name                        = local.name
  cluster_name                = local.name
  vpc_cidr                    = var.vpc_cidr
  azs                         = var.azs
  public_subnet_cidrs         = var.public_subnet_cidrs
  private_subnet_cidrs        = var.private_subnet_cidrs
  single_nat_gateway          = var.cost_optimized
  enable_interface_endpoints  = !var.cost_optimized
  interface_endpoint_services = var.interface_endpoint_services
  tags                        = local.common_tags
}

module "route53" {
  source = "../../modules/route-53"

  create_hosted_zone = var.create_route53_hosted_zone
  hosted_zone_name   = var.domain_name
  hosted_zone_id     = var.route53_hosted_zone_id
  tags               = local.common_tags
}

module "ecr" {
  for_each = local.ecr_repositories
  source   = "../../modules/ecr"

  repository_name = "${local.name}-${each.key}"
  force_delete    = false
  tags            = local.common_tags
}

module "eks" {
  source = "../../modules/eks"

  cluster_name                         = local.name
  kubernetes_version                   = var.kubernetes_version
  vpc_id                               = module.vpc.vpc_id
  private_subnet_ids                   = module.vpc.private_subnet_ids
  load_balancer_security_group_id      = module.vpc.load_balancer_security_group_id
  cluster_endpoint_public_access       = var.cluster_endpoint_public_access
  cluster_endpoint_public_access_cidrs = var.cluster_endpoint_public_access_cidrs
  shared_node_pool                     = var.cost_optimized
  system_node_pool = var.cost_optimized ? {
    instance_types = ["r5a.large"]
    min_size       = 1
    max_size       = 1
    desired_size   = 1
    disk_size      = 20
    capacity_type  = "ON_DEMAND"
  } : var.system_node_pool
  serving_node_pool               = var.serving_node_pool
  training_node_pool              = var.training_node_pool
  access_entries                  = local.access_entries
  bootstrap_cluster_creator_admin = var.platform_admin_principal_arn == null
  kms_key_administrators          = var.platform_admin_principal_arn == null ? var.kms_key_administrators : distinct(concat([var.platform_admin_principal_arn], var.kms_key_administrators))
  tags                            = local.common_tags
}

module "platform_data" {
  source = "../../modules/s3"

  bucket_name                        = "${local.name}-${data.aws_caller_identity.current.account_id}-data"
  noncurrent_version_expiration_days = null
  object_lock_enabled                = true
  object_lock_retention_days         = var.artifact_retention_days
  tags                               = local.common_tags
}

resource "aws_security_group" "postgresql" {
  name        = "${local.name}-postgresql"
  description = "PostgreSQL access from EKS workloads"
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.common_tags, {
    Name = "${local.name}-postgresql"
  })
}

resource "aws_vpc_security_group_ingress_rule" "postgresql_from_eks" {
  security_group_id            = aws_security_group.postgresql.id
  description                  = "PostgreSQL from EKS managed nodes"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = module.eks.node_security_group_id
  tags                         = local.common_tags
}

module "mlflow_database" {
  source = "../../modules/rds"

  database_identifier          = "${local.name}-mlflow"
  database_name                = "mlflow"
  username                     = "mlflow_admin"
  manage_master_user_password  = true
  instance_class               = var.cost_optimized ? "db.t4g.micro" : var.database_instance_class
  allocated_storage            = 20
  max_allocated_storage        = 100
  subnet_ids                   = module.vpc.private_subnet_ids
  security_group_ids           = [aws_security_group.postgresql.id]
  backup_retention_period      = var.database_backup_retention_days
  multi_az                     = !var.cost_optimized
  performance_insights_enabled = !var.cost_optimized
  deletion_protection          = true
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${local.name}-mlflow-final"
  tags                         = local.common_tags
}

module "airflow_database" {
  source = "../../modules/rds"

  database_identifier          = "${local.name}-airflow"
  database_name                = "airflow"
  username                     = "airflow_admin"
  manage_master_user_password  = true
  instance_class               = var.cost_optimized ? "db.t4g.micro" : var.database_instance_class
  allocated_storage            = 20
  max_allocated_storage        = 100
  subnet_ids                   = module.vpc.private_subnet_ids
  security_group_ids           = [aws_security_group.postgresql.id]
  backup_retention_period      = var.database_backup_retention_days
  multi_az                     = !var.cost_optimized
  performance_insights_enabled = !var.cost_optimized
  deletion_protection          = true
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${local.name}-airflow-final"
  tags                         = local.common_tags
}

module "redis" {
  source = "../../modules/redis"

  replication_group_id       = "${local.name}-features"
  vpc_id                     = module.vpc.vpc_id
  subnet_ids                 = module.vpc.private_subnet_ids
  allowed_security_group_ids = [module.eks.node_security_group_id]
  node_type                  = var.cost_optimized ? "cache.t4g.micro" : var.redis_node_type
  replicas_per_node_group    = var.cost_optimized ? 0 : 1
  transit_encryption_enabled = true
  tags                       = local.common_tags
}

data "aws_iam_policy_document" "pod_identity_assume_role" {
  for_each = local.pod_identities

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-namespace"
      values   = [each.value.namespace]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-service-account"
      values   = [each.value.service_account]
    }
  }
}

resource "aws_iam_role" "pod_identity" {
  for_each = local.pod_identities

  name               = "${local.name}-${replace(each.key, "_", "-")}"
  description        = "EKS Pod Identity role for ${each.value.namespace}/${each.value.service_account}"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume_role[each.key].json
  tags               = local.common_tags
}

resource "aws_eks_pod_identity_association" "workloads" {
  for_each = local.pod_identities

  cluster_name    = module.eks.cluster_name
  namespace       = each.value.namespace
  service_account = each.value.service_account
  role_arn        = aws_iam_role.pod_identity[each.key].arn
}

data "aws_iam_policy_document" "external_secrets" {
  statement {
    sid     = "ReadPlatformDatabaseCredentials"
    effect  = "Allow"
    actions = ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"]
    resources = [
      module.mlflow_database.master_user_secret_arn,
      module.airflow_database.master_user_secret_arn,
    ]
  }
}

resource "aws_iam_role_policy" "external_secrets" {
  name   = "read-platform-database-credentials"
  role   = aws_iam_role.pod_identity["external_secrets"].id
  policy = data.aws_iam_policy_document.external_secrets.json
}

data "aws_iam_policy_document" "inference_artifacts" {
  statement {
    sid       = "ListMlflowArtifacts"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [module.platform_data.bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${trimsuffix(var.mlflow_artifact_prefix, "/")}/*"]
    }
  }

  statement {
    sid       = "ReadMlflowArtifacts"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${module.platform_data.bucket_arn}/${trimsuffix(var.mlflow_artifact_prefix, "/")}/*"]
  }
}

resource "aws_iam_role_policy" "inference_artifacts" {
  name   = "read-mlflow-artifacts"
  role   = aws_iam_role.pod_identity["inference"].id
  policy = data.aws_iam_policy_document.inference_artifacts.json
}

data "aws_iam_policy_document" "mlflow_artifacts" {
  statement {
    sid       = "ListArtifactBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [module.platform_data.bucket_arn]
  }

  statement {
    sid    = "ManageMlflowArtifacts"
    effect = "Allow"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListMultipartUploadParts",
      "s3:PutObject",
    ]
    resources = ["${module.platform_data.bucket_arn}/${trimsuffix(var.mlflow_artifact_prefix, "/")}/*"]
  }
}

resource "aws_iam_role_policy" "mlflow_artifacts" {
  name   = "manage-mlflow-artifacts"
  role   = aws_iam_role.pod_identity["mlflow"].id
  policy = data.aws_iam_policy_document.mlflow_artifacts.json
}

data "aws_iam_policy_document" "airflow_data" {
  statement {
    sid       = "ListPlatformData"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [module.platform_data.bucket_arn]
  }

  statement {
    sid    = "ManageTrainingData"
    effect = "Allow"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListMultipartUploadParts",
      "s3:PutObject",
    ]
    resources = [
      "${module.platform_data.bucket_arn}/data/*",
      "${module.platform_data.bucket_arn}/monitoring/*",
      "${module.platform_data.bucket_arn}/snapshots/*",
    ]
  }
}

resource "aws_iam_role_policy" "airflow_data" {
  name   = "manage-training-data"
  role   = aws_iam_role.pod_identity["airflow"].id
  policy = data.aws_iam_policy_document.airflow_data.json
}

resource "aws_iam_policy" "load_balancer_controller" {
  name        = "${local.name}-load-balancer-controller"
  description = "Upstream AWS Load Balancer Controller v3.6.0 policy matching the pinned Helm chart"
  # https://github.com/kubernetes-sigs/aws-load-balancer-controller/blob/v3.6.0/docs/install/iam_policy.json
  policy = file("${path.module}/policies/aws-load-balancer-controller.json")
  tags   = local.common_tags
}

resource "aws_iam_role_policy_attachment" "load_balancer_controller" {
  role       = aws_iam_role.pod_identity["aws_load_balancer_controller"].name
  policy_arn = aws_iam_policy.load_balancer_controller.arn
}

data "aws_iam_policy_document" "cluster_autoscaler" {
  statement {
    sid    = "ScaleTaggedNodeGroups"
    effect = "Allow"
    actions = [
      "autoscaling:SetDesiredCapacity",
      "autoscaling:TerminateInstanceInAutoScalingGroup",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/k8s.io/cluster-autoscaler/${module.eks.cluster_name}"
      values   = ["owned"]
    }
  }

  statement {
    sid    = "DiscoverNodeGroups"
    effect = "Allow"
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeAutoScalingInstances",
      "autoscaling:DescribeLaunchConfigurations",
      "autoscaling:DescribeScalingActivities",
      "autoscaling:DescribeTags",
      "ec2:DescribeImages",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeLaunchTemplateVersions",
      "ec2:GetInstanceTypesFromInstanceRequirements",
      "eks:DescribeNodegroup",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "cluster_autoscaler" {
  name   = "scale-production-node-groups"
  role   = aws_iam_role.pod_identity["cluster_autoscaler"].id
  policy = data.aws_iam_policy_document.cluster_autoscaler.json
}

data "aws_iam_policy_document" "external_dns" {
  statement {
    sid       = "ChangeProductionDns"
    effect    = "Allow"
    actions   = ["route53:ChangeResourceRecordSets"]
    resources = ["arn:${data.aws_partition.current.partition}:route53:::hostedzone/${module.route53.hosted_zone_id}"]
  }

  statement {
    sid    = "DiscoverHostedZones"
    effect = "Allow"
    actions = [
      "route53:ListHostedZones",
      "route53:ListResourceRecordSets",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "external_dns" {
  name   = "manage-production-dns"
  role   = aws_iam_role.pod_identity["external_dns"].id
  policy = data.aws_iam_policy_document.external_dns.json
}
