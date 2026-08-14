output "region" {
  value = var.region
}

output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.this.endpoint
}

output "oidc_issuer_url" {
  description = "EKS OIDC issuer — the trust anchor for IRSA."
  value       = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

output "oidc_provider_arn" {
  description = "IAM OIDC provider ARN registered from the issuer (what IRSA roles trust)."
  value       = aws_iam_openid_connect_provider.this.arn
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "private_subnet_ids" {
  description = "Private subnets the node group and pod IPs live in."
  value       = aws_subnet.private[*].id
}

output "get_credentials_command" {
  description = "Fetch kubeconfig for this cluster. --alias names the context after the cluster so it matches the installer's kubeContext (default is the long cluster ARN)."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${aws_eks_cluster.this.name} --alias ${aws_eks_cluster.this.name}"
}

# --- storage / IRSA (null when enable_storage_identity = false) ---

output "db_bucket" {
  description = "Shared DB data-plane bucket -> chart db-slim PINECONE_BLOB_STORE__*_BUCKET_NAME."
  value       = one(module.storage_identity[*].db_bucket)
}

output "nexus_buckets" {
  description = "Map of nexus logical store -> bucket name -> chart config.storage.<store>."
  value       = one(module.storage_identity[*].nexus_buckets)
}

output "bucket_names" {
  description = "All seven buckets (DB + six nexus), informational."
  value       = one(module.storage_identity[*].bucket_names)
}

output "irsa_role_arn" {
  description = "Nexus workload IRSA role ARN -> each blob-accessing SA's eks.amazonaws.com/role-arn annotation."
  value       = one(module.storage_identity[*].irsa_role_arn)
}

output "alb_controller_role_arn" {
  description = "AWS Load Balancer Controller IRSA role ARN (null unless enable_load_balancer_controller)."
  value       = one(aws_iam_role.alb_controller[*].arn)
}
