variable "blob_prefix" {
  description = "Stem the seven bucket names derive from: <stem>-db plus six <stem>-nexus-* (source, knowledge, archive, traces, snapshots, library). A random suffix is appended for S3 global uniqueness."
  type        = string
}

variable "oidc_provider_arn" {
  description = "ARN of the cluster's IAM OIDC provider the IRSA role trusts."
  type        = string
}

variable "oidc_provider_url" {
  description = "Issuer URL of the cluster's OIDC provider (with https://); the condition keys derive from its host."
  type        = string
}

variable "role_name" {
  description = "Name of the IRSA role the Nexus workload service accounts assume."
  type        = string
}

variable "service_accounts" {
  description = "Namespace/service-account subjects trusted to assume the IRSA role."
  type = list(object({
    namespace       = string
    service_account = string
  }))
  default = []
}
