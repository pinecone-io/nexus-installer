# A default StorageClass so PVCs that name no class (e.g. FoundationDB's volumeClaimTemplate)
# get an EBS gp3 volume. EKS ships no working default class (the in-tree gp2 provisioner is
# gone in current Kubernetes), so without this stateful pods stay Pending on unbound PVCs.

data "aws_eks_cluster_auth" "this" {
  name = aws_eks_cluster.this.name
}

provider "kubernetes" {
  host                   = aws_eks_cluster.this.endpoint
  cluster_ca_certificate = base64decode(aws_eks_cluster.this.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.this.token
}

resource "kubernetes_storage_class" "gp3_default" {
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }
  storage_provisioner = "ebs.csi.aws.com"
  # Wait so the volume lands in the AZ the pod is scheduled to (nodes span >= 2 AZs).
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true
  parameters = {
    type = "gp3"
  }

  depends_on = [aws_eks_addon.ebs_csi]
}
