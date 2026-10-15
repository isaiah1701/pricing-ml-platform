locals {
  github_subject_repository = coalesce(var.github_oidc_repository, var.github_repository, "disabled")
}

resource "aws_s3_bucket" "terraform_state" {
  bucket = local.state_bucket_name

  tags = merge(local.common_tags, {
    Name = local.state_bucket_name
  })
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.github_repository == null ? 0 : 1

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github[0].certificates[0].sha1_fingerprint]

  tags = merge(local.common_tags, {
    Name = "github-actions"
  })
}

data "aws_iam_policy_document" "terraform_execution_assume_role" {
  statement {
    sid     = "TrustedOperators"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = var.terraform_trusted_principal_arns
    }
  }

  dynamic "statement" {
    for_each = var.github_repository == null ? [] : [var.github_repository]

    content {
      sid     = "GitHubActions"
      effect  = "Allow"
      actions = ["sts:AssumeRoleWithWebIdentity"]

      principals {
        type = "Federated"
        identifiers = [
          aws_iam_openid_connect_provider.github[0].arn
        ]
      }

      condition {
        test     = "StringEquals"
        variable = "token.actions.githubusercontent.com:aud"
        values   = ["sts.amazonaws.com"]
      }

      condition {
        test     = "StringLike"
        variable = "token.actions.githubusercontent.com:sub"
        values = [
          "repo:${local.github_subject_repository}:ref:refs/heads/main",
          "repo:${local.github_subject_repository}:ref:refs/tags/v*",
          "repo:${local.github_subject_repository}:environment:${var.github_environment}"
        ]
      }
    }
  }
}

resource "aws_iam_role" "terraform_execution" {
  name                 = "${local.name_prefix}-terraform"
  description          = "Terraform execution role for the ${local.name_prefix} MLOps platform"
  assume_role_policy   = data.aws_iam_policy_document.terraform_execution_assume_role.json
  max_session_duration = 3600

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-terraform"
  })
}

data "aws_iam_policy_document" "terraform_execution" {
  statement {
    sid     = "ReadEnvironmentStateBucket"
    effect  = "Allow"
    actions = ["s3:ListBucket"]

    resources = [aws_s3_bucket.terraform_state.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.state_key}*"]
    }
  }

  statement {
    sid    = "ManageEnvironmentState"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]

    resources = ["${aws_s3_bucket.terraform_state.arn}/${local.state_key}"]
  }

  statement {
    sid    = "ManageEnvironmentStateLock"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]

    resources = ["${aws_s3_bucket.terraform_state.arn}/${local.state_key}.tflock"]
  }

  # These are the regional AWS services used by the environment and reusable
  # MLOps modules. IAM and Route 53 permissions are scoped separately below.
  statement {
    sid    = "ManageMlopsRegionalInfrastructure"
    effect = "Allow"
    actions = [
      "ec2:*",
      "ecr:*",
      "eks:*",
      "kms:*",
      "logs:*",
      "rds:*",
      "elasticache:*",
      "secretsmanager:*",
      "autoscaling:CreateOrUpdateTags",
      "autoscaling:DeleteTags",
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeTags",
    ]

    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.aws_region]
    }
  }

  statement {
    sid    = "ManageEnvironmentDns"
    effect = "Allow"
    actions = [
      "route53:ChangeResourceRecordSets",
      "route53:ChangeTagsForResource",
      "route53:CreateHostedZone",
      "route53:DeleteHostedZone",
      "route53:GetChange",
      "route53:GetHostedZone",
      "route53:ListHostedZones",
      "route53:ListHostedZonesByName",
      "route53:ListResourceRecordSets",
      "route53:ListTagsForResource",
    ]

    resources = ["*"]
  }

  statement {
    sid    = "ManageEnvironmentDataBuckets"
    effect = "Allow"
    actions = [
      "s3:CreateBucket",
      "s3:DeleteBucket",
      "s3:GetBucketLocation",
      "s3:GetBucketPolicy",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketPublicAccessBlock",
      "s3:GetBucketTagging",
      "s3:GetBucketVersioning",
      "s3:GetEncryptionConfiguration",
      "s3:GetLifecycleConfiguration",
      "s3:ListBucket",
      "s3:PutBucketPolicy",
      "s3:DeleteBucketPolicy",
      "s3:PutBucketObjectLockConfiguration",
      "s3:PutBucketPublicAccessBlock",
      "s3:PutBucketTagging",
      "s3:PutBucketVersioning",
      "s3:PutEncryptionConfiguration",
      "s3:PutLifecycleConfiguration",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::${local.name_prefix}-*",
    ]
  }

  statement {
    sid    = "ManageEnvironmentIamRolesAndPolicies"
    effect = "Allow"
    actions = [
      "iam:AttachRolePolicy",
      "iam:CreatePolicy",
      "iam:CreatePolicyVersion",
      "iam:CreateRole",
      "iam:DeletePolicy",
      "iam:DeletePolicyVersion",
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:DetachRolePolicy",
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListPolicyTags",
      "iam:ListPolicyVersions",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
      "iam:PutRolePolicy",
      "iam:TagPolicy",
      "iam:TagRole",
      "iam:UntagPolicy",
      "iam:UntagRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:UpdateRoleDescription",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-*",
      "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:policy/${local.name_prefix}-*",
    ]
  }

  statement {
    sid    = "ManageEnvironmentEksOidcProvider"
    effect = "Allow"
    actions = [
      "iam:CreateOpenIDConnectProvider",
      "iam:DeleteOpenIDConnectProvider",
      "iam:GetOpenIDConnectProvider",
      "iam:TagOpenIDConnectProvider",
      "iam:UntagOpenIDConnectProvider",
      "iam:UpdateOpenIDConnectProviderThumbprint",
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/oidc.eks.${var.aws_region}.amazonaws.com/id/*",
    ]
  }

  statement {
    sid     = "ReadAccountIamMetadata"
    effect  = "Allow"
    actions = ["iam:ListOpenIDConnectProviders", "iam:ListPolicies", "iam:ListRoles"]

    resources = ["*"]
  }

  statement {
    sid     = "CreatePlatformServiceLinkedRoles"
    effect  = "Allow"
    actions = ["iam:CreateServiceLinkedRole"]

    resources = [
      "arn:${data.aws_partition.current.partition}:iam::*:role/aws-service-role/eks.amazonaws.com/AWSServiceRoleForAmazonEKS",
      "arn:${data.aws_partition.current.partition}:iam::*:role/aws-service-role/eks-nodegroup.amazonaws.com/AWSServiceRoleForAmazonEKSNodegroup",
      "arn:${data.aws_partition.current.partition}:iam::*:role/aws-service-role/rds.amazonaws.com/AWSServiceRoleForRDS",
      "arn:${data.aws_partition.current.partition}:iam::*:role/aws-service-role/elasticache.amazonaws.com/AWSServiceRoleForElastiCache",
      "arn:${data.aws_partition.current.partition}:iam::*:role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling",
      "arn:${data.aws_partition.current.partition}:iam::*:role/aws-service-role/spot.amazonaws.com/AWSServiceRoleForEC2Spot",
    ]

    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values = [
        "autoscaling.amazonaws.com",
        "elasticache.amazonaws.com",
        "eks.amazonaws.com",
        "eks-nodegroup.amazonaws.com",
        "rds.amazonaws.com",
        "spot.amazonaws.com",
      ]
    }
  }

  statement {
    sid     = "PassEnvironmentRolesToEks"
    effect  = "Allow"
    actions = ["iam:PassRole"]

    resources = [
      "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-*",
    ]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["eks.amazonaws.com", "ec2.amazonaws.com", "pods.eks.amazonaws.com"]
    }
  }

  statement {
    sid     = "IdentifyTerraformCaller"
    effect  = "Allow"
    actions = ["sts:GetCallerIdentity"]

    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "terraform_execution" {
  name   = "${local.name_prefix}-terraform-platform"
  role   = aws_iam_role.terraform_execution.id
  policy = data.aws_iam_policy_document.terraform_execution.json
}

data "aws_iam_policy_document" "github_ecr_publish_assume_role" {
  count = var.github_repository == null ? 0 : 1

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github[0].arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${local.github_subject_repository}:environment:${var.github_environment}"]
    }
  }
}

resource "aws_iam_role" "github_ecr_publish" {
  count = var.github_repository == null ? 0 : 1

  name                 = "${local.name_prefix}-github-ecr-publish"
  description          = "Publish immutable production images from the protected GitHub Environment"
  assume_role_policy   = data.aws_iam_policy_document.github_ecr_publish_assume_role[0].json
  max_session_duration = 3600
  tags                 = local.common_tags
}

data "aws_iam_policy_document" "github_ecr_publish" {
  count = var.github_repository == null ? 0 : 1

  statement {
    sid       = "AuthenticateToEcr"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "PublishProductionImages"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [
      "arn:${data.aws_partition.current.partition}:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/${local.name_prefix}-serving",
      "arn:${data.aws_partition.current.partition}:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/${local.name_prefix}-training",
    ]
  }
}

resource "aws_iam_role_policy" "github_ecr_publish" {
  count = var.github_repository == null ? 0 : 1

  name   = "publish-production-images"
  role   = aws_iam_role.github_ecr_publish[0].id
  policy = data.aws_iam_policy_document.github_ecr_publish[0].json
}
