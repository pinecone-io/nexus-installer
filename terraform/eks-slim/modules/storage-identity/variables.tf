variable "bucket_name" {
  description = "Globally-unique S3 bucket name (null -> composed <prefix>-<random> at the root)."
  type        = string
}

variable "blob_prefix" {
  description = "Stem the seven Nexus data-path key prefixes derive from (<stem>-db plus six <stem>-nexus-*). Feeds the chart's blob.s3 prefix; itself a stem, not a single prefix."
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
