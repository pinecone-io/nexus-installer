output "bucket_name" {
  description = "S3 bucket holding the Nexus data path -> chart storage bucket value."
  value       = aws_s3_bucket.nexus.bucket
}

output "bucket_arn" {
  value = aws_s3_bucket.nexus.arn
}

output "blob_prefix" {
  description = "Key-prefix stem the seven Nexus data-path prefixes derive from."
  value       = var.blob_prefix
}

output "prefix_names" {
  description = "The seven key prefixes provisioned from the stem (informational; the chart writes objects underneath)."
  value       = local.prefix_names
}

output "irsa_role_arn" {
  description = "IRSA role ARN -> each blob-accessing SA's eks.amazonaws.com/role-arn annotation."
  value       = aws_iam_role.nexus_workload.arn
}
