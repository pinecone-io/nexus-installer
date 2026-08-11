# Blob storage account + the seven containers the Nexus data path requires, the Nexus
# workload user-assigned identity, its Storage Blob Data Contributor grant, and the
# workload-identity federated credentials.
#
# The umbrella chart consumes these via blob.abs.{account,container} plus the workload
# identity SA annotation (client_id). Everything here is optional at the root (a customer
# can bring their own account/identity and skip this module).

# All seven DB blob stores (DATA/DOCS/BACKUP/WAL/JANITOR/INTERNAL/GLACIER) share the single
# <stem>-db container — their keys never collide, so the DB needs one container, not seven.
locals {
  container_suffixes = [
    "db",
    "nexus-source",
    "nexus-knowledge",
    "nexus-archive",
    "nexus-traces",
    "nexus-snapshots",
    "nexus-library",
  ]
  container_names = [for s in local.container_suffixes : "${var.container_prefix}-${s}"]
}

resource "random_string" "suffix" {
  length  = 6
  lower   = true
  upper   = false
  numeric = true
  special = false
}

# StorageV2 account. Global names must be 3-24 chars, lowercase alphanumeric.
resource "azurerm_storage_account" "nexus" {
  name                     = substr("${var.storage_account_prefix}${random_string.suffix.result}", 0, 24)
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_kind             = "StorageV2"
  account_tier             = "Standard"
  account_replication_type = "LRS"
  access_tier              = "Hot"

  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = true
  min_tls_version                 = "TLS1_2"

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 3
    }
  }

  # Reachable from the AKS subnet over the Microsoft.Storage service endpoint.
  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"]
    virtual_network_subnet_ids = [var.aks_subnet_id]
  }

  tags = var.tags
}

resource "azurerm_storage_container" "nexus" {
  for_each              = toset(local.container_names)
  name                  = each.value
  storage_account_id    = azurerm_storage_account.nexus.id
  container_access_type = "private"
}

# Nexus workload identity — the pods' Azure identity for blob access.
resource "azurerm_user_assigned_identity" "nexus_workload" {
  name                = var.workload_identity_name
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

# Blob data access for the workload identity, scoped to this account.
resource "azurerm_role_assignment" "nexus_blob_contributor" {
  scope                = azurerm_storage_account.nexus.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.nexus_workload.principal_id
  principal_type       = "ServicePrincipal"
}

# One federated credential per namespace/service-account pair, trusting the AKS OIDC issuer.
resource "azurerm_federated_identity_credential" "nexus" {
  for_each            = { for fc in var.federated_credentials : fc.name => fc }
  name                = each.value.name
  resource_group_name = var.resource_group_name
  parent_id           = azurerm_user_assigned_identity.nexus_workload.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = var.oidc_issuer_url
  subject             = "system:serviceaccount:${each.value.namespace}:${each.value.service_account}"
}
