# Resource group, VNet, and the single AKS node subnet — deliberately minimal: no NAT
# gateway / static egress IPs, no delegated or private-link subnets. Egress uses the AKS
# standard load balancer.

resource "azurerm_resource_group" "this" {
  name     = local.rg_name
  location = var.location
  tags     = local.tags
}

resource "azurerm_virtual_network" "this" {
  name                = "vnet-${local.cluster_name}"
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  address_space       = var.vnet_address_space
  tags                = local.tags
}

# Node subnet. Under Azure CNI Overlay pods live off-subnet, so a /27 is sufficient. The
# Microsoft.Storage service endpoint lets nodes reach the blob account privately.
resource "azurerm_subnet" "aks" {
  name                 = "snet-aks"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.aks_subnet_prefix]
  service_endpoints    = ["Microsoft.Storage"]
}
