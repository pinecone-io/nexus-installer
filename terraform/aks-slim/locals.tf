# Composed resource names: <type>-<name_prefix>-<environment>, e.g. rg-nexus-slim-dev.
# "slim" is the deployment config (the slim installer's cluster); environment is a knob
# so the same module stands up dev/staging/prod instances without editing names.
locals {
  base           = "${var.name_prefix}-${var.environment}"
  rg_name        = coalesce(var.resource_group_name, "rg-${local.base}")
  cluster_name   = coalesce(var.cluster_name, "aks-${local.base}")
  dns_prefix     = coalesce(var.dns_prefix, local.base)
  storage_prefix = coalesce(var.storage_account_prefix, replace(local.base, "-", ""))
  tags           = merge(var.tags, { environment = var.environment })
}
