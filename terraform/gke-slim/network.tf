# A custom-mode VPC with one subnet carrying three ranges: the primary (node IPs) plus two
# secondary ranges GKE consumes for VPC-native alias IPs (pods and Services). Sized so the
# pod range — the real density cap, since GKE allocates a /24 of it per node — holds far more
# than the node count. A Cloud Router + Cloud NAT give private-mode nodes egress (image
# pulls, GCS, control-plane reach); public-mode nodes keep their own external IPs but the NAT
# is harmless and keeps the two modes symmetric.

resource "google_compute_network" "this" {
  name                    = "vpc-${local.cluster_name}"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "this" {
  name          = "snet-${local.cluster_name}"
  network       = google_compute_network.this.id
  region        = var.region
  ip_cidr_range = var.subnet_cidr

  # Alias-IP ranges GKE references by name (ip_allocation_policy in gke.tf).
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pods_cidr
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.services_cidr
  }

  # Pods reach Google APIs (GCS, Artifact Registry) over the subnet's Private Google Access
  # route, so private-mode nodes need no public path for the data plane.
  private_ip_google_access = true
}

resource "google_compute_router" "this" {
  name    = "rt-${local.cluster_name}"
  network = google_compute_network.this.id
  region  = var.region
}

resource "google_compute_router_nat" "this" {
  name                               = "nat-${local.cluster_name}"
  router                             = google_compute_router.this.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}
