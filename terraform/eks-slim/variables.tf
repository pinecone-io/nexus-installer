# ---- Account / region -----------------------------------------------------

variable "region" {
  description = "AWS region. Some regions lack the chosen instance type or an AZ count; verify before changing."
  type        = string
  default     = "us-east-1"
}

# ---- Naming ---------------------------------------------------------------
# Resources are named <type>-<name_prefix>-<environment> (e.g. eks-nexus-slim-dev).
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
  description = "Override the composed EKS cluster name. Null -> eks-<name_prefix>-<environment>."
  type        = string
  default     = null
}

variable "tags" {
  description = <<-EOT
    Extra tags the installer wants on every taggable resource. Merged on top of the
    baseline (config, managed-by, environment) and applied via the provider default_tags,
    so setting this augments rather than replaces those.
  EOT
  type        = map(string)
  default     = {}
}

# ---- Cluster --------------------------------------------------------------

variable "kubernetes_version" {
  description = <<-EOT
    EKS Kubernetes version, pinned to a minor (e.g. "1.31"); EKS selects the patch.
    Set to the customer's target so the cluster mirrors their real environment.
  EOT
  type        = string
  default     = "1.31"
}

# ---- Node group (deliberately vanilla — mirrors a BYO customer cluster) -----
# No nexus-role labels/taints and no extra groups: the cluster must exercise the
# empty-nodeSelector scheduling path. A nexus-role group here would hide that bug.

variable "node_count" {
  description = "Desired node count for the single managed node group. Provisional pending load sizing."
  type        = number
  default     = 2
}

variable "node_min_count" {
  description = "Minimum node count for the managed node group."
  type        = number
  default     = 2
}

variable "node_max_count" {
  description = "Maximum node count for the managed node group."
  type        = number
  default     = 3
}

variable "node_instance_type" {
  description = "Instance type for the node group. m6i.2xlarge = 8 vCPU / 32 GiB. Provisional pending load sizing."
  type        = string
  default     = "m6i.2xlarge"
}

variable "node_disk_size_gb" {
  description = "EBS root volume size (GiB) per node."
  type        = number
  default     = 100
}

# ---- Networking (AWS VPC CNI) ---------------------------------------------
# The VPC CNI assigns pod IPs out of the node subnet, so the subnet must hold every pod,
# not just every node. Private subnets are /20 (4094 IPs) per AZ. EKS requires subnets
# in >= 2 AZs.

variable "vpc_cidr" {
  description = "VPC CIDR. Must be large enough for the per-AZ /20 private subnets plus the public subnets."
  type        = string
  default     = "10.224.0.0/16"
}

variable "az_count" {
  description = "Number of AZs to spread subnets across. EKS requires >= 2."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2
    error_message = "EKS requires subnets in at least 2 availability zones."
  }
}

variable "private_subnet_newbits" {
  description = <<-EOT
    Prefix-length delta from the VPC CIDR to each private (node/pod) subnet. 4 over a /16
    yields /20 (4094 IPs), sized for real pod density under the VPC CNI.
  EOT
  type        = number
  default     = 4
}

variable "public_subnet_newbits" {
  description = "Prefix-length delta from the VPC CIDR to each public subnet (NAT egress / optional ALB). 8 over a /16 yields /24."
  type        = number
  default     = 8
}

variable "enable_prefix_delegation" {
  description = <<-EOT
    Turn on VPC CNI prefix delegation (ENABLE_PREFIX_DELEGATION on the aws-node addon).
    Assigns /28 prefixes per ENI instead of secondary IPs, raising pods-per-node well
    past the ENI-times-IP default and stretching subnet IPs further.
  EOT
  type        = bool
  default     = true
}

# ---- Optional storage + IRSA identity (own sub-module) --------------------
# A customer with an existing S3 bucket + IAM role turns this off and wires their own
# into the chart's storage config.

variable "enable_storage_identity" {
  description = "Create the seven Nexus data-path S3 buckets, the IRSA role, and its S3 access policy."
  type        = bool
  default     = true
}

variable "blob_prefix" {
  description = <<-EOT
    Stem the seven bucket names derive from: <stem>-db plus six <stem>-nexus-* (source,
    knowledge, archive, traces, snapshots, library). The suffixes are a fixed product
    contract, so the operator supplies only the stem; a random suffix is appended for S3
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
  description = "Namespace the chart installs into; the derived IRSA trust binds blob SAs here."
  type        = string
  default     = "nexus"
}

variable "workload_service_accounts" {
  description = "Extra service accounts appended to the derived IRSA-trusted set. Empty for a standard install; for a non-default release/namespace set helm_release_name/helm_namespace instead."
  type = list(object({
    namespace       = string
    service_account = string
  }))
  default = []
}

# ---- Optional ingress -----------------------------------------------------
# Validate via `kubectl port-forward svc/nexus-gateway 80`. Flip this on to install the
# AWS Load Balancer Controller so it can double as the customer ingress (ALB) reference.

variable "enable_load_balancer_controller" {
  description = "Install the AWS Load Balancer Controller IAM policy + IRSA role for ALB ingress. Off for port-forward validation."
  type        = bool
  default     = false
}
