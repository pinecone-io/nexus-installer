# nexus-installer

Deploy Pinecone Nexus into a Kubernetes cluster you operate.

Nexus requires a Pinecone Enterprise plan. Pinecone distributes the container images and
the OCI chart, and provides the registry access you pull them from. Contact your
Pinecone account team before you begin.

## Contents

- `install/` — the install toolkit. Fill in one inputs file, then generate the Helm
  values, run preflight, confirm the images are in your registry, and install. Consumes
  the published OCI chart. See `install/README.md`.
- `terraform/<cloud>-slim/` — optional Terraform for a ready-made cluster with pre-created
  object storage and keyless workload identity. Skip any of these if you already have a
  cluster; they provision infrastructure only and are not required by the install. One per
  cloud:

  | Module | Cloud | Cluster | Object storage | Keyless identity |
  |---|---|---|---|---|
  | `terraform/aks-slim/` | Azure | AKS | Blob containers | Workload Identity (federated credentials) |
  | `terraform/eks-slim/` | AWS | EKS | S3 buckets | IRSA |
  | `terraform/gke-slim/` | GCP | GKE | GCS buckets | Workload Identity |

  Each module's `README.md` has the details.

The Helm chart is published as an OCI artifact alongside the image bundle;
`image-manifest.sh` lists what to stage in your registry and `install.sh` consumes the
chart. A local-chart path is available for a non-default embedding dimension or a
freshly minted index id.
