# Bucket-per-store, because pc-blob maps each logical store to a whole bucket (there is no
# bucket+prefix mode): the DB shares one bucket across its seven stores (their keys are
# disjoint) and each nexus store gets its own. S3 has no account namespace, so every name
# carries the random stem to stay globally unique.

locals {
  nexus_stores = ["source", "knowledge", "archive", "traces", "snapshots", "library"]

  stem = "${var.blob_prefix}-${random_string.suffix.result}"

  # Keyed by a static store id (known at plan time) so the bucket for_each is stable; the
  # name itself embeds the random stem and is only known after apply, which is fine for a
  # resource argument but would break a for_each key.
  bucket_names = merge(
    { "db" = "${local.stem}-db" },
    { for s in local.nexus_stores : "nexus-${s}" => "${local.stem}-nexus-${s}" },
  )

  oidc_host = replace(var.oidc_provider_url, "https://", "")
}

# One suffix shared by all seven names, so they group under a single stem.
resource "random_string" "suffix" {
  length  = 6
  lower   = true
  upper   = false
  numeric = true
  special = false
}

resource "aws_s3_bucket" "nexus" {
  for_each = local.bucket_names
  bucket   = each.value
}

resource "aws_s3_bucket_versioning" "nexus" {
  for_each = aws_s3_bucket.nexus
  bucket   = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "nexus" {
  for_each                = aws_s3_bucket.nexus
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "nexus" {
  for_each = aws_s3_bucket.nexus
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---- IRSA role -----------------------------------------------------------
# A single role trusted by every blob-accessing service account (see locals.tf for the set).

data "aws_iam_policy_document" "assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Multiple subjects in one StringEquals = trust ANY of them (OR).
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = [for sa in var.service_accounts : "system:serviceaccount:${sa.namespace}:${sa.service_account}"]
    }
  }
}

resource "aws_iam_role" "nexus_workload" {
  name               = var.role_name
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
}

data "aws_iam_policy_document" "s3_access" {
  statement {
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [for b in aws_s3_bucket.nexus : b.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListMultipartUploadParts", "s3:AbortMultipartUpload"]
    resources = [for b in aws_s3_bucket.nexus : "${b.arn}/*"]
  }
}

resource "aws_iam_role_policy" "s3_access" {
  name   = "s3-access"
  role   = aws_iam_role.nexus_workload.id
  policy = data.aws_iam_policy_document.s3_access.json
}
