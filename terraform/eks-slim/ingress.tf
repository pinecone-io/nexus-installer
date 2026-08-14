# Optional managed ingress. EKS has no in-cluster managed ingress, so the terraform-only
# piece is the IRSA role + IAM policy the AWS Load Balancer Controller needs. When enabled,
# this stages that identity; installing the controller Helm chart (which annotates its
# service account with the role and then provisions ALBs) is a separate step. Off by
# default: `kubectl port-forward svc/nexus-gateway 80:80` is the default validation path.

data "aws_iam_policy_document" "alb_controller_assume_role" {
  count = var.enable_load_balancer_controller ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.this.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")}:sub"
      values   = ["system:serviceaccount:kube-system:aws-load-balancer-controller"]
    }
  }
}

resource "aws_iam_role" "alb_controller" {
  count              = var.enable_load_balancer_controller ? 1 : 0
  name               = "role-${local.cluster_name}-alb"
  assume_role_policy = data.aws_iam_policy_document.alb_controller_assume_role[0].json
}

# The canonical upstream AWS Load Balancer Controller policy, vendored so the module has no
# network dependency at apply time. Refresh from the controller release that matches your
# Helm install if AWS adds new required actions.
resource "aws_iam_policy" "alb_controller" {
  count  = var.enable_load_balancer_controller ? 1 : 0
  name   = "policy-${local.cluster_name}-alb"
  policy = file("${path.module}/iam-policy-alb-controller.json")
}

resource "aws_iam_role_policy_attachment" "alb_controller" {
  count      = var.enable_load_balancer_controller ? 1 : 0
  role       = aws_iam_role.alb_controller[0].name
  policy_arn = aws_iam_policy.alb_controller[0].arn
}
