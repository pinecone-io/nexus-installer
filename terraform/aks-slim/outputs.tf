output "resource_group_name" {
  value = azurerm_resource_group.this.name
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.this.name
}

output "oidc_issuer_url" {
  description = "AKS OIDC issuer — the trust anchor for workload-identity federated creds."
  value       = azurerm_kubernetes_cluster.this.oidc_issuer_url
}

output "node_resource_group" {
  description = "AKS-managed node resource group (holds the VMSS, disks, LB)."
  value       = azurerm_kubernetes_cluster.this.node_resource_group
}

output "get_credentials_command" {
  description = "Fetch kubeconfig for this cluster."
  value       = "az aks get-credentials --resource-group ${azurerm_resource_group.this.name} --name ${azurerm_kubernetes_cluster.this.name}"
}

# --- storage / workload identity (null when enable_storage_identity = false) ---

output "blob_storage_account" {
  description = "Blob account name -> chart blob.abs.account."
  value       = one(module.storage_identity[*].storage_account_name)
}

output "blob_containers" {
  description = "Blob containers -> chart blob.abs.container."
  value       = one(module.storage_identity[*].container_names)
}

output "workload_identity_client_id" {
  description = "Nexus workload UAMI client ID -> workload-identity service-account annotation."
  value       = one(module.storage_identity[*].workload_identity_client_id)
}
