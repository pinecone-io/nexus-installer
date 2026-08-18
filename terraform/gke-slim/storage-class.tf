# GKE already ships a working default StorageClass (`standard-rwo`, pd-balanced), so unnamed
# PVCs — FoundationDB's volumeClaimTemplate — bind without any class defined here. This
# optional pd-ssd class is offered for pods that name it explicitly and is deliberately NOT
# marked default, so it never collides with GKE's built-in default.

data "google_client_config" "this" {}

provider "kubernetes" {
  host                   = "https://${google_container_cluster.this.endpoint}"
  cluster_ca_certificate = base64decode(google_container_cluster.this.master_auth[0].cluster_ca_certificate)
  token                  = data.google_client_config.this.access_token
}

resource "kubernetes_storage_class" "ssd" {
  count = var.create_ssd_storage_class ? 1 : 0

  metadata {
    name = "nexus-ssd"
  }
  storage_provisioner = "pd.csi.storage.gke.io"
  # Wait so the disk lands in the zone the pod is scheduled to (regional cluster spans zones).
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true
  parameters = {
    type = "pd-ssd"
  }

  depends_on = [google_container_node_pool.this]
}
