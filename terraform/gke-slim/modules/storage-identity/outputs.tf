output "bucket_prefix" {
  description = "Stem the seven bucket names derive from (<stem>-db + six <stem>-nexus-*) -> chart blob.gcs.bucketPrefix."
  value       = local.stem
}

output "gsa_email" {
  description = "GSA email each blob-accessing SA impersonates -> chart blob.gcs.serviceAccount (iam.gke.io/gcp-service-account)."
  value       = google_service_account.nexus_blob.email
}

output "db_bucket" {
  description = "The shared DB data-plane bucket (informational; derives as <bucket_prefix>-db)."
  value       = google_storage_bucket.nexus["db"].name
}

output "nexus_buckets" {
  description = "Map of nexus logical store -> bucket name -> chart config.storage.<store> / PINECONE_STORAGE__<STORE>."
  value       = { for s in local.nexus_stores : s => google_storage_bucket.nexus["nexus-${s}"].name }
}

output "bucket_names" {
  description = "All seven buckets (DB + six nexus), informational."
  value       = [for b in google_storage_bucket.nexus : b.name]
}
