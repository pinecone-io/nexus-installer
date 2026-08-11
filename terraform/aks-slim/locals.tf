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
}
