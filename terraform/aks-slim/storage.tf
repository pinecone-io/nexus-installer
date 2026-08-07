# Optional storage + workload identity, kept as its own sub-module so a customer who
# brings their own blob account + identity can drop it entirely (enable_storage_identity =
# false) and wire their own values into the chart's blob.abs.{account,container}.

module "storage_identity" {
  source = "./modules/storage-identity"
  count  = var.enable_storage_identity ? 1 : 0

  resource_group_name    = azurerm_resource_group.this.name
  location               = var.location
  storage_account_prefix = local.storage_prefix
  container_names        = var.blob_container_names
  aks_subnet_id          = azurerm_subnet.aks.id

  # Workload identity federates against the cluster's OIDC issuer.
  oidc_issuer_url        = azurerm_kubernetes_cluster.this.oidc_issuer_url
  federated_credentials  = var.workload_federated_credentials
  workload_identity_name = "id-${local.cluster_name}-nexus"

  tags = local.tags
}
