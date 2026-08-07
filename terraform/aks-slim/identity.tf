# AKS control-plane identity.
#
# Pre-created user-assigned (not system-assigned) because the cluster uses a BYO subnet:
# AKS needs Network Contributor on that subnet to attach node NICs and manage the load
# balancer, and a user-assigned identity can be granted that role before the cluster
# exists. System-assigned would be a chicken-and-egg on a custom subnet. AKS manages the
# kubelet identity itself.

resource "azurerm_user_assigned_identity" "aks_control_plane" {
  name                = "id-${local.cluster_name}-cp"
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  tags                = local.tags
}

resource "azurerm_role_assignment" "aks_subnet_network_contributor" {
  scope                = azurerm_subnet.aks.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_user_assigned_identity.aks_control_plane.principal_id
  principal_type       = "ServicePrincipal"
}
