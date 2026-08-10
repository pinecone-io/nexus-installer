# ---- Subscription / region ------------------------------------------------

variable "subscription_id" {
  description = "Target Azure subscription. Null -> ARM_SUBSCRIPTION_ID from the environment."
  type        = string
  default     = null
}

variable "location" {
  description = "Azure region. Some regions reject D-family SKUs or zones; verify before changing."
  type        = string
  default     = "westus3"
}

# ---- Naming ---------------------------------------------------------------
# Resources are named <type>-<name_prefix>-<environment> (e.g. rg-nexus-slim-dev).
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

variable "resource_group_name" {
  description = "Override the composed resource group name. Null -> rg-<name_prefix>-<environment>."
  type        = string
  default     = null
}

variable "tags" {
  description = <<-EOT
    Extra tags the installer wants on every taggable resource. Merged on top of the
    baseline (config, managed-by, environment), so setting this augments rather than
    replaces those. Azure does not support tags on subnets, role assignments, federated
    credentials, or storage containers — those resources stay untagged.
  EOT
  type        = map(string)
  default     = {}
}

# ---- Cluster --------------------------------------------------------------

variable "cluster_name" {
  description = "Override the composed AKS cluster name. Null -> aks-<name_prefix>-<environment>."
  type        = string
  default     = null
}

variable "dns_prefix" {
  description = "Override the AKS DNS prefix. Null -> <name_prefix>-<environment>."
  type        = string
  default     = null
}

variable "kubernetes_version" {
  description = <<-EOT
    AKS Kubernetes version, pinned to a minor (e.g. "1.35"); AKS selects the latest patch.
    Set to the customer's target so the cluster mirrors their real environment. Null lets
    AKS pick its own default supported version.
  EOT
  type        = string
  default     = "1.35"
}

variable "sku_tier" {
  description = "AKS SKU tier. Free (no uptime SLA) suits a PoC/dev instance; Standard for prod-like."
  type        = string
  default     = "Free"
}

# ---- Node pool (deliberately vanilla — mirrors a BYO customer cluster) -----
# No nexus-role labels/taints and no extra pools: the cluster must exercise the
# empty-nodeSelector scheduling path. A nexus-role pool here would hide that bug.

variable "node_count" {
  description = "Fixed node count for the single system pool. Provisional pending load sizing."
  type        = number
  default     = 2
}

variable "node_vm_size" {
  description = "VM size for the system pool. 2x D8s_v5 = 16 vCPU / 64 GiB. Provisional pending load sizing."
  type        = string
  default     = "Standard_D8s_v5"
}

variable "os_disk_size_gb" {
  description = "OS disk size (GiB) per node."
  type        = number
  default     = 100
}

variable "node_zones" {
  description = "Availability zones for the node pool. Null = single-AZ / no zone pinning (fine for PoC)."
  type        = list(string)
  default     = null
}

variable "max_pods" {
  description = "Max pods per node. 250 is the Overlay default."
  type        = number
  default     = 250
}

# ---- Networking (Azure CNI Overlay) ---------------------------------------
# Overlay puts pods on pod_cidr (off-subnet), so a /27 node subnet is sufficient.
# /27 is the documented floor.

variable "vnet_address_space" {
  description = "VNet address space."
  type        = list(string)
  default     = ["10.224.0.0/16"]
}

variable "aks_subnet_prefix" {
  description = "Node subnet prefix. /27 is the documented floor under Overlay."
  type        = string
  default     = "10.224.0.0/27"
}

variable "pod_cidr" {
  description = "Overlay pod CIDR (off-subnet). Must not overlap the VNet."
  type        = string
  default     = "10.244.0.0/16"
}

variable "service_cidr" {
  description = "Kubernetes service CIDR. Must not overlap the VNet or pod_cidr."
  type        = string
  default     = "10.96.0.0/16"
}

variable "dns_service_ip" {
  description = "Cluster DNS service IP. Must sit inside service_cidr."
  type        = string
  default     = "10.96.0.10"
}

# ---- Optional storage + workload identity (own sub-module) ----------------
# Mirrors "customer brings their own storage": a customer with an existing blob
# account + identity can disable this and wire their own into the chart's blob.abs.

variable "enable_storage_identity" {
  description = "Create the Blob storage account, container(s), Nexus workload UAMI, role, and federated creds."
  type        = bool
  default     = true
}

variable "storage_account_prefix" {
  description = "Override the storage account name prefix. Null -> <name_prefix><environment> (hyphens stripped); a random suffix is appended."
  type        = string
  default     = null
}

variable "blob_container_names" {
  description = "Blob containers to create for Nexus (chart blob.abs.container points at one of these)."
  type        = list(string)
  default     = ["nexus"]
}

variable "workload_federated_credentials" {
  description = <<-EOT
    Azure Workload Identity federated credentials binding the Nexus workload UAMI to
    Kubernetes service accounts via the AKS OIDC issuer. Each entry creates one credential
    with subject system:serviceaccount:<namespace>:<service_account>.

    These must match the Helm release namespace and the service account the umbrella chart
    creates (the chart installs single-namespace; SA defaults to pinecone.serviceAccount.name).
    Leave empty to stand up the UAMI + storage now and add the binding once the SA name is
    fixed at install.
  EOT
  type = list(object({
    name            = string
    namespace       = string
    service_account = string
  }))
  default = []
}

# ---- Optional ingress -----------------------------------------------------
# Validate via `kubectl port-forward svc/nexus-gateway 80`. Flip this on to add the AKS
# managed ingress (app routing) so it can double as the customer ingress reference.

variable "enable_web_app_routing" {
  description = "Enable the AKS web_app_routing (managed NGINX) add-on. Off for port-forward validation."
  type        = bool
  default     = false
}
