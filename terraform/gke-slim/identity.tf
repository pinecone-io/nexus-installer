# The node pool's Google service account. GKE's default is the project's Compute Engine
# default SA, which carries broad Editor by inheritance; a dedicated node SA with only the
# logging/monitoring/registry roles is least-privilege. Pod-level GCS access is NOT here — it
# comes from Workload Identity (modules/storage-identity), so the node SA never holds bucket
# permissions.

resource "google_service_account" "node" {
  account_id   = "gke-${var.environment}-node"
  display_name = "Node SA for ${local.cluster_name}"
}

# The minimal roles a GKE node needs: ship logs and metrics, and pull images from Artifact
# Registry / GCR.
resource "google_project_iam_member" "node" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
    "roles/artifactregistry.reader",
  ])
  project = var.project
  role    = each.value
  member  = "serviceAccount:${google_service_account.node.email}"
}
