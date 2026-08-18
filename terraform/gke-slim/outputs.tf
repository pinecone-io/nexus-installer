output "project" {
  value = var.project
}

output "region" {
  value = var.region
}

output "cluster_name" {
  value = google_container_cluster.this.name
}

output "cluster_endpoint" {
  value = google_container_cluster.this.endpoint
}

output "workload_pool" {
  description = "The cluster's Workload Identity pool (<project>.svc.id.goog) — the trust anchor for the blob GSA bindings."
  value       = google_container_cluster.this.workload_identity_config[0].workload_pool
}

output "network_name" {
  value = google_compute_network.this.name
}

output "subnet_name" {
  description = "Node subnet; pod and Service IPs come from its `pods`/`services` secondary ranges."
  value       = google_compute_subnetwork.this.name
}

output "kube_context" {
  description = "kubectl context name `gcloud container clusters get-credentials` creates -> the installer's kubeContext."
  value       = "gke_${var.project}_${var.region}_${google_container_cluster.this.name}"
}

output "get_credentials_command" {
  description = "Fetch kubeconfig for this cluster; the context it writes matches the kube_context output."
  value       = "gcloud container clusters get-credentials ${google_container_cluster.this.name} --region ${var.region} --project ${var.project}"
}

output "ingress_ip" {
  description = "Reserved global external IP for a GCE Ingress (null unless enable_ingress)."
  value       = one(google_compute_global_address.ingress[*].address)
}

# --- storage / Workload Identity (null when enable_storage_identity = false) ---

output "bucket_prefix" {
  description = "Bucket-name stem -> chart blob.gcs.bucketPrefix (the DB + six nexus buckets derive from it)."
  value       = one(module.storage_identity[*].bucket_prefix)
}

output "gsa_email" {
  description = "Nexus blob GSA email -> chart blob.gcs.serviceAccount, annotated onto every blob-accessing SA."
  value       = one(module.storage_identity[*].gsa_email)
}

output "db_bucket" {
  description = "Shared DB data-plane bucket (informational; derives as <bucket_prefix>-db)."
  value       = one(module.storage_identity[*].db_bucket)
}

output "nexus_buckets" {
  description = "Map of nexus logical store -> bucket name -> chart config.storage.<store>."
  value       = one(module.storage_identity[*].nexus_buckets)
}

output "bucket_names" {
  description = "All seven buckets (DB + six nexus), informational."
  value       = one(module.storage_identity[*].bucket_names)
}
