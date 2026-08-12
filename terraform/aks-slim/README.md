# terraform/aks-slim — optional turnkey AKS cluster (slim install)

Stands up a **deliberately vanilla** AKS cluster for a self-managed Nexus **slim** install. It
mirrors a customer's own Kubernetes cluster so the install exercises the real path — in
particular the **empty-nodeSelector** scheduling behaviour: there are **no `nexus-role` node
labels or taints** and no dedicated Nexus pools, so a regression there surfaces here instead of
hiding.

**Optional by design.** A customer bringing their own cluster (and/or their own blob storage +
identity) skips this — it is not a dependency of the chart. It is the executable twin of the
self-managed prerequisites.

## Naming

Resources are named `<type>-<name_prefix>-<environment>` — e.g. `rg-nexus-slim-dev`,
`aks-nexus-slim-dev`. `slim` is the deployment config; `environment` (default `dev`) is a
per-instance knob, so the same module stands up `staging`/`prod` instances without editing
names. Any individual name can still be overridden (`resource_group_name`, `cluster_name`, ...).

## What it creates

- Resource group, VNet, and a single `/27` node subnet — `/27` is sufficient because Azure CNI
  **Overlay** puts pods off-subnet.
- AKS: single-AZ, **one vanilla system pool** (2× `Standard_D8s_v5`, provisional pending load
  sizing), Overlay networking, OIDC issuer + workload identity enabled, standard-LB egress.
- User-assigned control-plane identity, pre-granted `Network Contributor` on the subnet (required
  for a BYO subnet; avoids the system-assigned chicken-and-egg).
- **Optional** (`enable_storage_identity`, default on) storage + workload-identity sub-module:
  a StorageV2 blob account + the **seven** blob containers the Nexus data path requires, the
  Nexus workload identity, its `Storage Blob Data Contributor` grant, and
  per-namespace/service-account federated credentials. The seven containers are derived from a
  single stem (`blob_container_prefix`): `<stem>-db` (the whole DB data plane shares this one
  container) plus six `<stem>-nexus-*` (`source`, `knowledge`, `archive`, `traces`, `snapshots`,
  `library`). The suffix set is a fixed product contract — the operator supplies only the stem,
  so no container is hand-enumerated and no manual container-creation step is needed.

## Prerequisites

- `az login` with access to the target subscription; `terraform >= 1.5`.
- D-family vCPU quota in the region (2× D8s_v5 = 16 vCPU).
- A region that offers the chosen SKU and zones (verify before changing `location`).

## Usage

```bash
export ARM_SUBSCRIPTION_ID=<your-subscription-id>   # or rely on your az login default
cp terraform.tfvars.example terraform.tfvars        # adjust as needed
terraform init
terraform plan
terraform apply
```

Then fetch credentials and confirm the BYO-mirror shape:

```bash
$(terraform output -raw get_credentials_command)
kubectl get nodes
# expect: NO nexus-role labels present
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.labels}{"\n"}{end}' | grep -i nexus-role || echo "OK: no nexus-role labels"
```

Validate a workload without ingress via `kubectl port-forward svc/nexus-gateway 80:80`
(set `enable_web_app_routing = true` to add the managed ingress instead).

## Wiring into the chart

After apply, feed the outputs into the umbrella chart's Azure Blob (`blob.abs`) values:

| Output | Chart value |
|---|---|
| `blob_storage_account` | `blob.abs.account` |
| `blob_container` | `blob.abs.container` (a **stem**, not one container — the chart derives the seven names from it) |
| `workload_identity_client_id` | workload-identity service-account annotation |

The seven containers themselves (`blob_container_names`, informational) are created by this
module, so the chart's data path is ready as soon as `terraform apply` completes — there is no
separate container-creation step. (A forthcoming chart release will rename the chart value
`blob.abs.container` → `containerPrefix`; the `blob_container` output name should follow once
it lands.)

The `workload_federated_credentials` variable must match the Helm **release namespace** and the
**service account** the chart creates (single-namespace install; SA = `pinecone.serviceAccount.name`).
Leave the variable empty to stand the infra up now and add the binding once the SA name is fixed
at install time.
