# nexus-installer

Self-hosted Nexus installer — deploy Pinecone Nexus into a **customer-operated
Kubernetes cluster** (in your own data center or your own cloud account). This is the
self-hosted counterpart to Pinecone-managed BYOC (Pulumi) and the fully managed cloud.

> **Nexus requires a Pinecone Enterprise plan.** The container images and OCI chart this
> installer deploys are distributed by Pinecone to Enterprise customers — Pinecone provides
> the release bundle and the registry access it mirrors from. Talk to your Pinecone account
> team before you begin.

## Contents

- `install/` — the **customer-runnable install toolkit**: fill one inputs file
  (`customer.example.yaml`), then generate the Helm values, validate the consistency
  invariants (preflight), mirror the image bundle, and install — self-serve or
  hands-on. It consumes the published OCI chart; see `install/README.md`.
- `terraform/aks-slim/` — **optional** Terraform for a turnkey AKS cluster on Azure for the
  slim install (environment-parameterized: dev/staging/prod from one module). A customer with
  an existing cluster ignores it; a customer wanting a ready-made cluster can `terraform apply`
  it. It is **not** a dependency of the install — it only stands up infrastructure the chart
  then installs onto. See its README for details.

_The umbrella Helm chart is built and published as an OCI artifact alongside the image
bundle; you consume it via `mirror.sh` + `install.sh`. The local-chart path is only for a
non-default embedding dimension or a freshly minted index id, where Pinecone provides a
chart checkout._
