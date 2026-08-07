# terraform/aks-slim — optional turnkey AKS cluster (slim install)

Stands up a **deliberately vanilla** AKS cluster for a self-managed Nexus **slim** install. It
mirrors a customer's own Kubernetes cluster so the install exercises the real path — in
particular the **empty-nodeSelector** behaviour (#1562): there are **no `nexus-role` node labels
or taints** and no dedicated Nexus pools, so a regression there surfaces here instead of hiding.

**Optional by design.** A customer bringing their own cluster (and/or their own blob storage +
identity) skips this — it is not a dependency of the chart. It is the executable twin of the
self-managed prerequisites doc (#1577).

## Naming

Resources are named `<type>-<name_prefix>-<environment>` — e.g. `rg-nexus-slim-dev`,
`aks-nexus-slim-dev`. `slim` is the deployment config; `environment` (default `dev`) is a
per-instance knob, so the same module stands up `staging`/`prod` instances without editing
names. Any individual name can still be overridden (`resource_group_name`, `cluster_name`, ...).

## What it creates

- Resource group, VNet, and a single `/27` node subnet — `/27` is sufficient because Azure CNI
  **Overlay** puts pods off-subnet (#1571).
- AKS: single-AZ, **one vanilla system pool** (2× `Standard_D8s_v5`, provisional pending #1574),
  Overlay networking, OIDC issuer + workload identity enabled, standard-LB egress.
- User-assigned control-plane identity, pre-granted `Network Contributor` on the subnet (required
  for a BYO subnet; avoids the system-assigned chicken-and-egg).
- **Optional** (`enable_storage_identity`, default on) storage + workload-identity sub-module:
  a StorageV2 blob account + container(s), the Nexus workload identity, its `Storage Blob Data
  Contributor` grant, and per-namespace/service-account federated credentials.

## Prerequisites

- `az login` with access to the target subscription; `terraform >= 1.5`.
- D-family vCPU quota in the region (2× D8s_v5 = 16 vCPU; westus3 default is 350).
- Region: **westus3** or **centralus** (eastus/westus2 fail on SKU/zone).

## Usage

```bash
source ../../../.byoc.azure.local.env      # exports ARM_SUBSCRIPTION_ID etc.
cp terraform.tfvars.example terraform.tfvars   # adjust as needed
terraform init
terraform plan
terraform apply
```

Then fetch credentials and confirm the BYO-mirror shape:

```bash
$(terraform output -raw get_credentials_command)
kubectl get nodes
# expect: NO nexus-role labels present (the #1562 validation)
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.labels}{"\n"}{end}' | grep -i nexus-role || echo "OK: no nexus-role labels"
```

Validate a workload without ingress via `kubectl port-forward svc/nexus-gateway 80:80`
(set `enable_web_app_routing = true` to add the managed ingress instead — #1572).

## Wiring into the chart

After apply, feed the outputs into the umbrella chart's Azure Blob (`blob.abs`) values:

| Output | Chart value |
|---|---|
| `blob_storage_account` | `blob.abs.account` |
| `blob_containers[*]` | `blob.abs.container` |
| `workload_identity_client_id` | workload-identity service-account annotation |

The `workload_federated_credentials` variable must match the Helm **release namespace** and the
**service account** the chart creates (single-namespace install; SA = `pinecone.serviceAccount.name`).
The Azure workload-identity SA annotations are Avi's #1570 lane — leave the variable empty to stand
the infra up now and add the binding once the SA name is fixed at install time.

> Reference: the resource shapes here were distilled from a live prod BYOC cell export
> (an internal prod BYOC cell export), stripped to the slim BYO-mirror shape
> (single vanilla pool, Overlay/`/27`, no NAT/DNS/KeyVault/Postgres/private-link).
