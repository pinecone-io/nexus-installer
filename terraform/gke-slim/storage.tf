# Optional storage + Workload Identity, kept as its own sub-module so a customer who brings
# their own bucket + GSA can drop it entirely (enable_storage_identity = false) and wire their
# own values into the chart's storage config.

module "storage_identity" {
  source = "./modules/storage-identity"
  count  = var.enable_storage_identity ? 1 : 0

  project       = var.project
  location      = var.region
  blob_prefix   = local.blob_prefix
  workload_pool = local.workload_pool

  gsa_account_id = "nexus-${var.environment}-blob"
  # The derived SA set (locals.tf) covers the chart's blob-accessing service accounts.
  service_accounts = local.effective_workload_service_accounts

  labels = local.labels
}
