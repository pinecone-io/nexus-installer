# S3 bucket + the seven Nexus data-path key prefixes, plus the IRSA role the workload
# service accounts assume for bucket access and its S3 policy.
#
# The umbrella chart consumes these via the bucket + prefix plus the IRSA role ARN
# annotated onto each service account. Everything here is optional at the root (a customer
# can bring their own bucket/role and skip this module).
#
# An S3 prefix is a key namespace that springs into being on first write, so the seven
# need no per-prefix resource. The zero-byte markers below exist only so the layout is
# visible (aws s3 ls) and the contract is asserted at apply time.

locals {
  # All seven DB blob stores (DATA/DOCS/BACKUP/WAL/JANITOR/INTERNAL/GLACIER) share the
  # single <stem>-db prefix — their keys never collide, so the DB needs one prefix, not seven.
  container_suffixes = [
    "db",
    "nexus-source",
    "nexus-knowledge",
    "nexus-archive",
    "nexus-traces",
    "nexus-snapshots",
    "nexus-library",
  ]
  prefix_names = [for s in local.container_suffixes : "${var.blob_prefix}-${s}"]

  # Strip the scheme so the OIDC host can key the trust-policy conditions.
  oidc_host = replace(var.oidc_provider_url, "https://", "")
}

resource "random_string" "suffix" {
  length  = 6
  lower   = true
  upper   = false
  numeric = true
  special = false
}

# Bucket names are globally unique, 3-63 chars, lowercase. The random suffix avoids
# collisions across environments/accounts.
resource "aws_s3_bucket" "nexus" {
  bucket = coalesce(var.bucket_name, "${var.blob_prefix}-${random_string.suffix.result}")
}

resource "aws_s3_bucket_versioning" "nexus" {
  bucket = aws_s3_bucket.nexus.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "nexus" {
  bucket                  = aws_s3_bucket.nexus.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "nexus" {
  bucket = aws_s3_bucket.nexus.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_object" "prefix_marker" {
  for_each = toset(local.prefix_names)
  bucket   = aws_s3_bucket.nexus.id
  key      = "${each.value}/"
  content  = ""
}

# ---- IRSA role -----------------------------------------------------------
# One role trusted by every Nexus blob-accessing service account. The chart annotates each
# SA with this role's ARN (eks.amazonaws.com/role-arn).

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
  # Bucket-level: list + locate.
  statement {
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.nexus.arn]
  }
  # Object-level: full read/write within the bucket.
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListMultipartUploadParts", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.nexus.arn}/*"]
  }
}

resource "aws_iam_role_policy" "s3_access" {
  name   = "s3-access"
  role   = aws_iam_role.nexus_workload.id
  policy = data.aws_iam_policy_document.s3_access.json
}
