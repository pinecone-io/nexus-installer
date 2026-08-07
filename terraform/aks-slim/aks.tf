# The slim-install AKS cluster.
#
# BYO-mirror on purpose: single vanilla system pool (NO nexus-role labels/taints, NO extra
# pools), single-AZ, Azure CNI Overlay, workload identity + OIDC issuer enabled so the
# Nexus workload UAMI can federate to in-cluster service accounts (storage.tf).

resource "azurerm_kubernetes_cluster" "this" {
  name                = local.cluster_name
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  dns_prefix          = local.dns_prefix
  kubernetes_version  = var.kubernetes_version
  sku_tier            = var.sku_tier
  node_resource_group = "${local.rg_name}-nodes"

  role_based_access_control_enabled = true
  oidc_issuer_enabled               = true
  workload_identity_enabled         = true

  # Single system pool. only_critical_addons_enabled is intentionally left false so
  # workloads schedule here on an untainted pool — the whole point of the BYO mirror.
  default_node_pool {
    name            = "system"
    vm_size         = var.node_vm_size
    node_count      = var.node_count
    os_disk_size_gb = var.os_disk_size_gb
    os_sku          = "Ubuntu"
    type            = "VirtualMachineScaleSets"
    zones           = var.node_zones
    max_pods        = var.max_pods
    vnet_subnet_id  = azurerm_subnet.aks.id
    # No node_labels/node_taints on purpose (mirror a BYO customer cluster).
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.aks_control_plane.id]
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_data_plane  = "azure"
    outbound_type       = "loadBalancer"
    load_balancer_sku   = "standard"
    service_cidr        = var.service_cidr
    dns_service_ip      = var.dns_service_ip
    pod_cidr            = var.pod_cidr
    ip_versions         = ["IPv4"]
  }

  dynamic "web_app_routing" {
    for_each = var.enable_web_app_routing ? [1] : []
    content {
      dns_zone_ids = []
    }
  }

  tags = local.tags

  # The subnet role assignment must land before the cluster provisions nodes/LB.
  depends_on = [azurerm_role_assignment.aks_subnet_network_contributor]

  lifecycle {
    # AKS bumps the patch version and rotates node images out of band; don't fight it.
    ignore_changes = [kubernetes_version, default_node_pool[0].orchestrator_version]
  }
}
