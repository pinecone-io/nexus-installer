# ---- Project / region -----------------------------------------------------

variable "project" {
  description = "GCP project id the cluster, network, buckets, and service account live in."
  type        = string
}

variable "region" {
  description = <<-EOT
    GCP region. The cluster is regional (control plane + nodes spread across the region's
    zones) for HA, mirroring the EKS module's multi-AZ shape. Verify the region offers the
    chosen machine type before changing.
  EOT
  type        = string
  default     = "us-central1"
}

# ---- Naming ---------------------------------------------------------------
# Resources are named <type>-<name_prefix>-<environment> (e.g. gke-nexus-slim-dev).
# environment is a per-instance knob so one module stands up dev/staging/prod without renaming.

variable "name_prefix" {
  description = "Deployment-config name prefix baked into every resource name."
  type        = string
  default     = "nexus-slim"
}

variable "environment" {
  description = "Environment/instance identifier appended to names (dev, staging, prod, ...)."
  type        = string
  default     = "dev"
}

variable "cluster_name" {
  description = "Override the composed GKE cluster name. Null -> gke-<name_prefix>-<environment>."
  type        = string
  default     = null
}

variable "labels" {
  description = <<-EOT
    Extra labels the installer wants on every labellable resource. Merged on top of the
    baseline (config, managed-by, environment). GCP label keys and values must be lowercase
    alphanumerics, `_`, or `-`.
  EOT
  type        = map(string)
  default     = {}
}

# ---- Cluster --------------------------------------------------------------

variable "kubernetes_version" {
  description = <<-EOT
    GKE control-plane + node version, applied to both the master and the node pool. Defaults to
    "1.35" — GKE resolves the highest 1.35.x the release channel offers (today 1.35.6-gke.1641000),
    which clears this module's minimum of 1.35.0-gke.3047000. Below that minimum the GKE Dataplane
    V2 (Cilium) agent can delete a live pod's CiliumEndpoint on a freshly created node, dropping its
    pod/DNS traffic until the pod is recreated (fixed builds: 1.33.11-gke.1137000+, 1.34.6-gke.1154000+,
    1.35.0-gke.3047000+). The fix must be on the node version, so it is pinned there too. Pin a fuller
    version only to mirror a customer's target; the validations below reject an affected build.
  EOT
  type        = string
  default     = "1.35"

  validation {
    condition     = can(regex("^1\\.(3[5-9]|[4-9][0-9])", var.kubernetes_version))
    error_message = "kubernetes_version must be 1.35 or newer; the GKE Dataplane V2 / Cilium stale-endpoint fix floor is 1.35.0-gke.3047000."
  }
  validation {
    # A 1.35.1+ build is a later train and always carries the fix; only an explicit 1.35.0-gke.N
    # build can be below it, so gate that case on the gke build number.
    condition     = !can(regex("^1\\.35\\.0-gke\\.", var.kubernetes_version)) || tonumber(regex("gke\\.(\\d+)", var.kubernetes_version)[0]) >= 3047000
    error_message = "A 1.35.0 build must be 1.35.0-gke.3047000 or later (the GKE Dataplane V2 / Cilium stale-endpoint fix)."
  }
}

variable "release_channel" {
  description = "GKE release channel that governs auto-upgrade cadence (RAPID, REGULAR, STABLE, or UNSPECIFIED to pin)."
  type        = string
  default     = "REGULAR"
}

# ---- Node pool (deliberately vanilla — mirrors a BYO customer cluster) ------
# No nexus-role labels/taints and no extra pools: the cluster must exercise the
# empty-nodeSelector scheduling path. A nexus-role pool here would hide that bug.

variable "node_count" {
  description = "Nodes per zone in the single node pool (a regional cluster multiplies this by the zone count). Provisional pending load sizing."
  type        = number
  default     = 1
}

variable "node_min_count" {
  description = "Autoscaling minimum nodes per zone."
  type        = number
  default     = 1
}

variable "node_max_count" {
  description = "Autoscaling maximum nodes per zone."
  type        = number
  default     = 2
}

variable "node_machine_type" {
  description = "Machine type for the node pool. e2-standard-8 = 8 vCPU / 32 GiB. Provisional pending load sizing."
  type        = string
  default     = "e2-standard-8"
}

variable "node_disk_size_gb" {
  description = "Boot disk size (GiB) per node."
  type        = number
  default     = 100
}

variable "node_disk_type" {
  description = "Boot disk type (pd-standard, pd-balanced, pd-ssd)."
  type        = string
  default     = "pd-balanced"
}

# ---- Networking (VPC-native / alias IPs) ----------------------------------
# GKE is VPC-native: nodes take IPs from the subnet primary range, pods from a secondary
# range, and Services from a second secondary range. GKE allocates a /24 of the pod range
# per node (110 pods/node default), so the pod range — not the node range — is what caps
# density. The three ranges below are independent, non-overlapping blocks; the defaults size
# the pod range for real density the way the EKS module sizes its /20 node subnet.

variable "subnet_cidr" {
  description = "Primary range of the node subnet (node IPs only). /20 = 4094 nodes, far above any node count."
  type        = string
  default     = "10.128.0.0/20"
}

variable "pods_cidr" {
  description = <<-EOT
    Secondary range for pod IPs (alias IPs). GKE carves a /24 per node from it, so this is the
    real density cap: a /16 (65 536 IPs) supports 256 nodes' worth of /24 blocks — generous
    headroom over the node count, mirroring the EKS pod-IP planning.
  EOT
  type        = string
  default     = "10.132.0.0/16"
}

variable "services_cidr" {
  description = "Secondary range for ClusterIP Services. /20 = 4094 Service IPs."
  type        = string
  default     = "10.130.0.0/20"
}

variable "master_ipv4_cidr" {
  description = "Reserved /28 for the private control-plane endpoints. Unused unless enable_private_nodes is on; must not overlap the ranges above."
  type        = string
  default     = "172.16.0.0/28"
}

variable "enable_private_nodes" {
  description = "Give nodes internal IPs only (egress via Cloud NAT). Off keeps the PoC simple; the control plane stays publicly reachable either way."
  type        = bool
  default     = false
}

# ---- Optional storage + Workload Identity (own sub-module) ----------------
# A customer with an existing GCS bucket + GSA turns this off and wires their own into the
# chart's storage config.

variable "enable_storage_identity" {
  description = "Create the seven Nexus data-path GCS buckets, the GSA, its bucket IAM, and the Workload Identity bindings."
  type        = bool
  default     = true
}

variable "blob_prefix" {
  description = <<-EOT
    Stem the seven bucket names derive from: <stem>-db plus six <stem>-nexus-* (source,
    knowledge, archive, traces, snapshots, library). The suffixes are a fixed product
    contract, so the operator supplies only the stem; a random suffix is appended for GCS
    global uniqueness. Null -> <name_prefix>-<environment>.
  EOT
  type        = string
  default     = null
}

variable "helm_release_name" {
  description = "Helm release name; the derived nexus-* blob SAs follow it. Match your `helm install`."
  type        = string
  default     = "nexus"
}

variable "helm_namespace" {
  description = "Namespace the chart installs into; the derived Workload Identity bindings bind blob SAs here."
  type        = string
  default     = "nexus"
}

variable "workload_service_accounts" {
  description = "Extra Kubernetes service accounts appended to the derived Workload-Identity-bound set. Empty for a standard install; for a non-default release/namespace set helm_release_name/helm_namespace instead."
  type = list(object({
    namespace       = string
    service_account = string
  }))
  default = []
}

# ---- Optional storage class -----------------------------------------------
# GKE already ships a working default StorageClass (`standard-rwo`, pd-balanced), so unlike
# EKS unnamed PVCs (FoundationDB's volumeClaimTemplate) bind out of the box. This optional
# pd-ssd class is available for pods that name it explicitly; it is NOT marked default, so it
# never collides with GKE's built-in default.

variable "create_ssd_storage_class" {
  description = "Create an additional (non-default) pd-ssd StorageClass named nexus-ssd for explicit use."
  type        = bool
  default     = false
}

# ---- Optional ingress -----------------------------------------------------
# Validate via `kubectl port-forward svc/nexus-gateway 80`. GKE's GCE ingress controller is
# built in (nothing to install); the only terraform-side piece is an optional reserved global
# IP the Ingress can adopt.

variable "enable_ingress" {
  description = "Reserve a global external IP for a future GCE Ingress. Off for port-forward validation; the Ingress object itself is a separate step."
  type        = bool
  default     = false
}
