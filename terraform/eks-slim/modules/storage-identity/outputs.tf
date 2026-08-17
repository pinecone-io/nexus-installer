output "bucket_prefix" {
  description = "Stem the seven bucket names derive from (<stem>-db + six <stem>-nexus-*) -> chart blob.s3.bucketPrefix."
  value       = local.stem
}

output "db_bucket" {
  description = "The shared DB data-plane bucket (informational; derives as <bucket_prefix>-db)."
  value       = aws_s3_bucket.nexus["db"].bucket
}

output "nexus_buckets" {
  description = "Map of nexus logical store -> bucket name -> chart config.storage.<store> / PINECONE_STORAGE__<STORE>."
  value       = { for s in local.nexus_stores : s => aws_s3_bucket.nexus["nexus-${s}"].bucket }
}

output "bucket_names" {
  description = "All seven buckets (DB + six nexus), informational."
  value       = [for b in aws_s3_bucket.nexus : b.bucket]
}

output "irsa_role_arn" {
  description = "IRSA role ARN -> each blob-accessing SA's eks.amazonaws.com/role-arn annotation."
  value       = aws_iam_role.nexus_workload.arn
}
