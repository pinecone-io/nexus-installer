# nexus-installer

Deploy Pinecone Nexus into a Kubernetes cluster you operate.

Nexus requires a Pinecone Enterprise plan. Pinecone distributes the container images and
the OCI chart, and provides the registry access this installer mirrors from. Contact your
Pinecone account team before you begin.

## Contents

- `install/` — the install toolkit. Fill in one inputs file, then generate the Helm
  values, run preflight, mirror the images, and install. Consumes the published OCI chart.
  See `install/README.md`.
- `terraform/aks-slim/` — optional Terraform for a ready-made AKS cluster on Azure. Skip
  it if you already have a cluster; it provisions infrastructure only and is not required
  by the install. See `terraform/aks-slim/README.md`.

The Helm chart is published as an OCI artifact alongside the image bundle; `mirror.sh` and
`install.sh` consume it. A local-chart path is available for a non-default embedding
dimension or a freshly minted index id.
