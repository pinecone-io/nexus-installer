# Bucket-per-store, because pc-blob maps each logical store to a whole bucket (there is no
# bucket+prefix mode): the DB shares one bucket across its seven stores (their keys are
# disjoint) and each nexus store gets its own. GCS bucket names are globally unique, so every
# name carries the random stem.

locals {
  nexus_stores = ["source", "knowledge", "archive", "traces", "snapshots", "library"]

  stem = "${var.blob_prefix}-${random_string.suffix.result}"

  # Keyed by a static store id (known at plan time) so the bucket for_each is stable; the
  # name itself embeds the random stem and is only known after apply.
  bucket_names = merge(
    { "db" = "${local.stem}-db" },
    { for s in local.nexus_stores : "nexus-${s}" => "${local.stem}-nexus-${s}" },
  )

  # roles/iam.workloadIdentityUser member per Kubernetes SA subject the chart runs.
  wi_members = {
    for sa in var.service_accounts :
    "${sa.namespace}/${sa.service_account}" =>
    "serviceAccount:${var.workload_pool}[${sa.namespace}/${sa.service_account}]"
  }
}

# One suffix shared by all seven names, so they group under a single stem.
resource "random_string" "suffix" {
  length  = 6
  lower   = true
  upper   = false
  numeric = true
  special = false
}

resource "google_storage_bucket" "nexus" {
  for_each = local.bucket_names

  name     = each.value
  project  = var.project
  location = var.location

  # Uniform bucket-level access: the Workload Identity IAM grant below is the only thing that
  # governs access; no per-object ACLs.
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  labels = var.labels

  # A validation-loop bucket must not block terraform destroy on leftover objects.
  force_destroy = true
}

# ---- Google service account + Workload Identity binding -------------------
# A single GSA every blob-accessing Kubernetes SA impersonates; each KSA is bound to it with
# roles/iam.workloadIdentityUser so its pods reach GCS keyless.

resource "google_service_account" "nexus_blob" {
  account_id   = var.gsa_account_id
  display_name = "Nexus blob-access GSA (${var.blob_prefix})"
  project      = var.project
}

resource "google_service_account_iam_member" "workload_identity" {
  for_each = local.wi_members

  service_account_id = google_service_account.nexus_blob.name
  role               = "roles/iam.workloadIdentityUser"
  member             = each.value
}

# The GSA's data-plane access, scoped to the seven buckets (not project-wide): read+write
# objects on each. Storage Object Admin covers get/put/delete/list within the bucket.
resource "google_storage_bucket_iam_member" "object_admin" {
  for_each = google_storage_bucket.nexus

  bucket = each.value.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.nexus_blob.email}"
}
