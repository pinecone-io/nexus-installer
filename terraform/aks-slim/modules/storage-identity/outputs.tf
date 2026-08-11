output "storage_account_name" {
  description = "Blob storage account name -> chart blob.abs.account."
  value       = azurerm_storage_account.nexus.name
}

output "container_prefix" {
  description = "Blob container stem -> chart blob.abs.container (a stem/prefix, not one container)."
  value       = var.container_prefix
}

output "container_names" {
  description = "The seven blob containers provisioned from the stem (informational)."
  value       = [for c in azurerm_storage_container.nexus : c.name]
}

output "workload_identity_client_id" {
  description = "Client ID of the Nexus workload UAMI -> workload-identity SA annotation."
  value       = azurerm_user_assigned_identity.nexus_workload.client_id
}

output "workload_identity_principal_id" {
  value = azurerm_user_assigned_identity.nexus_workload.principal_id
}
