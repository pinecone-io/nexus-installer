# AKS control-plane identity.
#
# We use a pre-created user-assigned identity (rather than system-assigned) for one
# concrete reason: the cluster uses a BYO subnet, and AKS needs Network Contributor on
# that subnet to attach node NICs and manage the load balancer. With system-assigned
# the identity only exists *after* cluster creation (chicken-and-egg on a custom subnet);
# with user-assigned we can grant the role first and depend on it. This mirrors the prod
# BYOC cell. The kubelet identity is left to AKS to manage (no separate UAMI needed here).

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
