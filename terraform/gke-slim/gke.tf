# The slim-install GKE cluster: VPC-native, Workload Identity enabled, with a single vanilla
# node pool (no nexus-role labels/taints, no extra pools). The vanilla pool is deliberate —
# workloads must schedule on it so an empty-nodeSelector regression surfaces instead of
# hiding. GKE ships CoreDNS, the GCE ingress controller, and a default StorageClass in-cluster,
# so there are no addon resources to declare.

resource "google_container_cluster" "this" {
  name     = local.cluster_name
  location = var.region

  network    = google_compute_network.this.id
  subnetwork = google_compute_subnetwork.this.id

  # Remove the default node pool GKE creates with the cluster and manage our own below, so
  # the node config (SA, Workload Identity metadata, machine type) is entirely ours.
  remove_default_node_pool = true
  initial_node_count       = 1

  min_master_version = var.kubernetes_version
  release_channel {
    channel = var.release_channel
  }

  # VPC-native: pods and Services come from the named secondary ranges on the subnet, not the
  # node range.
  networking_mode = "VPC_NATIVE"
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  # Workload Identity: bind Kubernetes SAs to Google SAs through this fixed pool. The pod
  # blob path (modules/storage-identity) rides on it for keyless GCS access.
  workload_identity_config {
    workload_pool = local.workload_pool
  }

  dynamic "private_cluster_config" {
    for_each = var.enable_private_nodes ? [1] : []
    content {
      enable_private_nodes    = true
      enable_private_endpoint = false
      master_ipv4_cidr_block  = var.master_ipv4_cidr
    }
  }

  # A real terraform destroy must not be blocked — this is a validation-loop cluster.
  deletion_protection = false

  resource_labels = local.labels

  lifecycle {
    ignore_changes = [initial_node_count]
  }
}

resource "google_container_node_pool" "this" {
  name     = "system"
  cluster  = google_container_cluster.this.id
  location = var.region

  # anetd (Cilium) runs on the nodes, so the Dataplane V2 stale-endpoint fix must be on the
  # node version — pin it here, not just on the master.
  version = var.kubernetes_version

  # Per-zone count; a regional cluster spreads the pool across the region's zones.
  initial_node_count = var.node_count
  autoscaling {
    min_node_count = var.node_min_count
    max_node_count = var.node_max_count
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.node_machine_type
    disk_size_gb    = var.node_disk_size_gb
    disk_type       = var.node_disk_type
    service_account = google_service_account.node.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    # GKE_METADATA turns on the metadata server that serves Workload Identity tokens to pods;
    # without it the KSA->GSA impersonation the blob path relies on cannot resolve.
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    labels = local.labels
    # No nexus-role labels/taints here on purpose (see comment above).
  }

  lifecycle {
    ignore_changes = [initial_node_count]
  }
}
