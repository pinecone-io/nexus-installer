# Resource group, VNet, and the single AKS node subnet.
# Deliberately minimal vs the prod BYOC cell: no NAT gateway / static egress IPs,
# no Postgres-delegated subnet, no private-link subnet. Egress goes through the
# AKS standard load balancer (outbound_type = loadBalancer in aks.tf).

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

# Node subnet. Under Azure CNI Overlay the pods live on pod_cidr (off-subnet), so a
# /27 here is sufficient (#1571). A Microsoft.Storage service endpoint lets the nodes
# reach the blob account privately (mirrors the prod cell).
resource "azurerm_subnet" "aks" {
  name                 = "snet-aks"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.aks_subnet_prefix]
  service_endpoints    = ["Microsoft.Storage"]
}
