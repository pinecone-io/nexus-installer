# The slim-install EKS cluster: a single vanilla managed node group (no nexus-role
# labels/taints, no extra groups) spread across the private subnets, with the core addons
# EKS does not ship in-cluster by default. The vanilla group is deliberate — workloads
# must schedule on it so an empty-nodeSelector regression surfaces instead of hiding.

resource "aws_eks_cluster" "this" {
  name     = local.cluster_name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = concat(aws_subnet.private[*].id, aws_subnet.public[*].id)
    endpoint_public_access  = true
    endpoint_private_access = true
  }

  # API auth so the terraform runner becomes cluster-admin without a manual aws-auth
  # ConfigMap edit.
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  depends_on = [aws_iam_role_policy_attachment.cluster]
}

# VPC CNI. Installed before the node group so nodes come up with pod networking. Prefix
# delegation (opt-in) hands each ENI a /28 instead of single secondary IPs, lifting
# pods-per-node far above the default and easing pressure on the /20 subnets.
resource "aws_eks_addon" "vpc_cni" {
  cluster_name  = aws_eks_cluster.this.name
  addon_name    = "vpc-cni"
  addon_version = null

  configuration_values = var.enable_prefix_delegation ? jsonencode({
    env = {
      ENABLE_PREFIX_DELEGATION = "true"
      WARM_PREFIX_TARGET       = "1"
    }
  }) : null

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "kube-proxy"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"
}

resource "aws_eks_node_group" "this" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "system"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id

  instance_types = [var.node_instance_type]
  disk_size      = var.node_disk_size_gb

  scaling_config {
    desired_size = var.node_count
    min_size     = var.node_min_count
    max_size     = var.node_max_count
  }

  update_config {
    max_unavailable = 1
  }

  # No labels/taints here on purpose (see comment above).

  depends_on = [
    aws_iam_role_policy_attachment.node,
    aws_eks_addon.vpc_cni,
  ]

  lifecycle {
    # The launch template / AMI release rotates out of band as EKS publishes new node
    # images — ignore that churn. version stays authoritative (pinned via var).
    ignore_changes = [scaling_config[0].desired_size]
  }
}

# CoreDNS needs schedulable nodes, so it lands after the node group.
resource "aws_eks_addon" "coredns" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "coredns"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.this]
}
