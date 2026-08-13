# Composed resource names: <type>-<name_prefix>-<environment>, e.g. rg-nexus-slim-dev.
# environment is a per-instance knob so one module stands up dev/staging/prod without renaming.
locals {
  base             = "${var.name_prefix}-${var.environment}"
  rg_name          = coalesce(var.resource_group_name, "rg-${local.base}")
  cluster_name     = coalesce(var.cluster_name, "aks-${local.base}")
  dns_prefix       = coalesce(var.dns_prefix, local.base)
  storage_prefix   = coalesce(var.storage_account_prefix, replace(local.base, "-", ""))
  container_prefix = coalesce(var.blob_container_prefix, local.base)
  # Baseline tags always apply; the installer's var.tags are merged on top (and may
  # override a baseline key if they choose to).
  tags = merge({
    config       = "slim"
    "managed-by" = "terraform"
    environment  = var.environment
  }, var.tags)

  # Every SA whose pods reach Blob under workload identity — unfederated ones 401. Keep in
  # sync with the chart (file-proxy shares nexus-api, task pods share nexus-orchestrator).
  blob_accessing_service_accounts = [
    "${var.helm_release_name}-api",
    "${var.helm_release_name}-orchestrator",
    "docs-api-sa",
    "index-builders-slab-sa",
    "query-routers-sa",
    "query-executors-slab-sa",
    "request-log-writers-sa",
  ]

  default_federated_credentials = [
    for sa in local.blob_accessing_service_accounts : {
      name            = sa
      namespace       = var.helm_namespace
      service_account = sa
    }
  ]

  # Derived defaults plus operator extras; keyed by name so an extra overrides on collision.
  effective_federated_credentials = values(merge(
    { for fc in local.default_federated_credentials : fc.name => fc },
    { for fc in var.workload_federated_credentials : fc.name => fc },
  ))
}
