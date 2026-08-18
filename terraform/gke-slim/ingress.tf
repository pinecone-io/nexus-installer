# Optional managed ingress. GKE's GCE ingress controller is built in — nothing to install — so
# the only terraform-side piece is an optional reserved global external IP an Ingress can adopt
# (via the `kubernetes.io/ingress.global-static-ip-name` annotation) so its address survives
# Ingress recreation. Off by default: `kubectl port-forward svc/nexus-gateway 80:80` is the
# default validation path, and the Ingress object plus a google_compute_managed_ssl_certificate
# (ManagedCertificate) are a separate step.

resource "google_compute_global_address" "ingress" {
  count = var.enable_ingress ? 1 : 0
  name  = "ip-${local.cluster_name}-ingress"
}
