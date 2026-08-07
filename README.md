# nexus-installer

Self-managed Nexus installer — deploy Pinecone Nexus into a **customer-operated
Kubernetes cluster** (on-prem or the customer's own cloud account). This is the
self-managed counterpart to Pinecone-managed BYOC (Pulumi) and the fully managed cloud.

## Contents

- `terraform/aks-slim/` — **optional** Terraform for a turnkey AKS cluster on Azure for the
  slim install (environment-parameterized: dev/staging/prod from one module). A customer with
  an existing cluster ignores it; a customer wanting a ready-made cluster can `terraform apply`
  it. It is **not** a dependency of the install — it only stands up infrastructure the chart
  then installs onto. See its README for details.

_The umbrella Helm chart currently lives in-tree at `nexus/deploy/installer/chart/`; its
migration into this repo is tracked separately._
