variable "project" {
  description = "GCP project the buckets and GSA live in."
  type        = string
}

variable "location" {
  description = "Bucket location (a region, e.g. us-central1)."
  type        = string
}

variable "blob_prefix" {
  description = "Stem the seven bucket names derive from: <stem>-db plus six <stem>-nexus-* (source, knowledge, archive, traces, snapshots, library). A random suffix is appended for GCS global uniqueness."
  type        = string
}

variable "workload_pool" {
  description = "The cluster's Workload Identity pool, <project>.svc.id.goog; the KSA->GSA member subjects derive from it."
  type        = string
}

variable "gsa_account_id" {
  description = "account_id of the Google service account every blob-accessing KSA impersonates."
  type        = string
}

variable "service_accounts" {
  description = "Namespace/service-account subjects bound to the GSA via roles/iam.workloadIdentityUser."
  type = list(object({
    namespace       = string
    service_account = string
  }))
  default = []
}

variable "labels" {
  description = "Labels applied to the buckets."
  type        = map(string)
  default     = {}
}
