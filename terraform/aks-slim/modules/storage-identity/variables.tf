variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "storage_account_prefix" {
  description = "Prefix for the globally-unique storage account name; a random suffix is appended."
  type        = string
}

variable "container_names" {
  description = "Blob containers to create."
  type        = list(string)
}

variable "aks_subnet_id" {
  description = "AKS node subnet id, allowed on the storage account network rules (service endpoint)."
  type        = string
}

variable "oidc_issuer_url" {
  description = "AKS OIDC issuer URL the federated credentials trust."
  type        = string
}

variable "workload_identity_name" {
  description = "Name of the user-assigned identity the Nexus workload federates to."
  type        = string
}

variable "federated_credentials" {
  description = "Federated identity credentials (namespace/service-account subjects)."
  type = list(object({
    name            = string
    namespace       = string
    service_account = string
  }))
  default = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
