locals {
  node_group_defaults = {
    ami_type      = "AL2023_x86_64_STANDARD"
    capacity_type = "ON_DEMAND"
    disk_size     = 50
    update_config = { max_unavailable_percentage = 33 }
    metadata_options = {
      http_endpoint               = "enabled"
      http_protocol_ipv6          = "disabled"
      http_put_response_hop_limit = 2
      http_tokens                 = "required"
      instance_metadata_tags      = "disabled"
    }
  }

  managed_node_groups = {
    system = merge(local.node_group_defaults, {
      instance_types = var.system_node_pool.instance_types
      capacity_type  = var.system_node_pool.capacity_type
      min_size       = var.system_node_pool.min_size
      max_size       = var.system_node_pool.max_size
      desired_size   = var.system_node_pool.desired_size
      disk_size      = var.system_node_pool.disk_size
      labels         = { workload = "system" }
    })

    serving = merge(local.node_group_defaults, {
      instance_types = var.serving_node_pool.instance_types
      capacity_type  = var.serving_node_pool.capacity_type
      min_size       = var.serving_node_pool.min_size
      max_size       = var.serving_node_pool.max_size
      desired_size   = var.serving_node_pool.desired_size
      disk_size      = var.serving_node_pool.disk_size
      labels         = { workload = "serving" }
      taints = {
        workload = {
          key    = "workload"
          value  = "serving"
          effect = "NO_SCHEDULE"
        }
      }
    })

    training = merge(local.node_group_defaults, {
      instance_types = var.training_node_pool.instance_types
      capacity_type  = var.training_node_pool.capacity_type
      min_size       = var.training_node_pool.min_size
      max_size       = var.training_node_pool.max_size
      desired_size   = var.training_node_pool.desired_size
      disk_size      = var.training_node_pool.disk_size
      labels         = { workload = "training" }
      taints = {
        workload = {
          key    = "workload"
          value  = "training"
          effect = "NO_SCHEDULE"
        }
      }
    })
  }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id                   = var.vpc_id
  subnet_ids               = var.private_subnet_ids
  control_plane_subnet_ids = var.private_subnet_ids

  endpoint_private_access      = true
  endpoint_public_access       = var.cluster_endpoint_public_access
  endpoint_public_access_cidrs = var.cluster_endpoint_public_access_cidrs

  authentication_mode                      = "API_AND_CONFIG_MAP"
  enable_cluster_creator_admin_permissions = var.bootstrap_cluster_creator_admin
  access_entries                           = var.access_entries

  create_kms_key          = true
  enable_kms_key_rotation = true
  kms_key_administrators  = var.kms_key_administrators
  encryption_config = {
    resources = ["secrets"]
  }

  enable_irsa = true

  enabled_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  node_security_group_additional_rules = {
    ingress_from_load_balancer = {
      description              = "Allow application load balancers to reach instance-mode targets"
      protocol                 = "tcp"
      from_port                = 30000
      to_port                  = 32767
      type                     = "ingress"
      source_security_group_id = var.load_balancer_security_group_id
    }
  }

  addons = {
    coredns = {}

    eks-pod-identity-agent = {
      before_compute = true
    }

    kube-proxy = {}

    vpc-cni = {
      before_compute = true
    }

    aws-ebs-csi-driver = {
      service_account_role_arn = aws_iam_role.ebs_csi_driver.arn
    }
  }

  eks_managed_node_groups = local.managed_node_groups

  tags = var.tags
}

data "aws_iam_policy_document" "ebs_csi_assume_role" {
  statement {
    effect = "Allow"

    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(module.eks.oidc_provider, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(module.eks.oidc_provider, "https://", "")}:sub"
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }
  }
}

resource "aws_iam_role" "ebs_csi_driver" {
  name               = "${var.cluster_name}-ebs-csi-driver"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi_driver" {
  role       = aws_iam_role.ebs_csi_driver.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# EKS node-group tags do not propagate to the underlying Auto Scaling groups.
resource "aws_autoscaling_group_tag" "discovery" {
  for_each = merge([
    for pool in keys(local.managed_node_groups) : {
      for key, value in {
        "k8s.io/cluster-autoscaler/enabled"                      = "true"
        "k8s.io/cluster-autoscaler/${var.cluster_name}"          = "owned"
        "k8s.io/cluster-autoscaler/node-template/label/workload" = pool
      } : "${pool}:${key}" => { pool = pool, key = key, value = value }
    }
  ]...)

  autoscaling_group_name = module.eks.eks_managed_node_groups[each.value.pool].node_group_autoscaling_group_names[0]
  tag {
    key                 = each.value.key
    value               = each.value.value
    propagate_at_launch = true
  }
}
