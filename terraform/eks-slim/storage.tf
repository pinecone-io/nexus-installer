# Optional storage + IRSA identity, kept as its own sub-module so a customer who brings
# their own buckets + IAM role can drop it entirely (enable_storage_identity = false) and
# wire their own values into the chart's storage/IRSA config.

module "storage_identity" {
  source = "./modules/storage-identity"
  count  = var.enable_storage_identity ? 1 : 0

  blob_prefix = local.blob_prefix

  # Trust anchor: the cluster's IAM OIDC provider; the derived SA set (locals.tf) covers
  # the chart's blob-accessing service accounts.
  oidc_provider_arn = aws_iam_openid_connect_provider.this.arn
  oidc_provider_url = aws_eks_cluster.this.identity[0].oidc[0].issuer
  role_name         = "role-${local.cluster_name}-nexus"
  service_accounts  = local.effective_workload_service_accounts
}
