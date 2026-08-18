# environment is a per-instance knob: one module stands up dev/staging/prod without renaming.
locals {
  base         = "${var.name_prefix}-${var.environment}"
  cluster_name = coalesce(var.cluster_name, "gke-${local.base}")
  blob_prefix  = coalesce(var.blob_prefix, local.base)

  workload_pool = "${var.project}.svc.id.goog"

  # Baseline labels always apply; var.labels merge on top. GCP labels must be lowercase.
  labels = merge({
    config       = "slim"
    "managed-by" = "terraform"
    environment  = var.environment
  }, var.labels)

  # Every SA whose pods reach GCS under Workload Identity — untrusted ones get 403. Keep in
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

  default_workload_service_accounts = [
    for sa in local.blob_accessing_service_accounts : {
      namespace       = var.helm_namespace
      service_account = sa
    }
  ]

  # Derived defaults plus operator extras; deduped by namespace/SA so an extra can't
  # double-list a derived subject.
  effective_workload_service_accounts = values(merge(
    { for sa in local.default_workload_service_accounts : "${sa.namespace}/${sa.service_account}" => sa },
    { for sa in var.workload_service_accounts : "${sa.namespace}/${sa.service_account}" => sa },
  ))
}
