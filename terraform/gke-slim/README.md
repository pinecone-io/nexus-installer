# terraform/gke-slim — optional turnkey GKE cluster (slim install)

Stands up a **deliberately vanilla** GKE cluster for a self-hosted Nexus **slim** install. It
looks like a customer's own Kubernetes cluster so the install exercises the real path — in
particular the **empty-nodeSelector** scheduling behaviour: there are **no `nexus-role` node
labels or taints** and no dedicated Nexus node pools, so a regression there surfaces here instead
of hiding.

**Optional by design.** A customer bringing their own cluster (and/or their own bucket + GSA)
skips this — it is not a dependency of the chart. It is the executable form of the self-hosted
prerequisites.

**Optional and decoupled.** This module is not a dependency of the chart or the install scripts;
`install/tf-to-inputs.sh` reads its outputs into `customer.yaml` when you use it. See
[object storage](#object-storage--bucket-per-store) for the layout it provisions.

## Naming

Resources are named `<type>-<name_prefix>-<environment>` — e.g. `gke-nexus-slim-dev`,
`vpc-gke-nexus-slim-dev`. `slim` is the deployment config; `environment` (default `dev`) is a
per-instance knob, so the same module stands up `staging`/`prod` instances without editing names.
Individual names can still be overridden (`cluster_name`, `blob_prefix`, ...).

## What it creates

- A custom-mode VPC with one subnet across `region`: a `/20` primary range (node IPs) plus two
  secondary ranges for VPC-native alias IPs — a `/16` for pods and a `/20` for Services. A Cloud
  Router + Cloud NAT give nodes egress. See [networking](#networking--sizing-the-pod-range) for
  why the pod range is the one sized large.
- GKE: a **regional** cluster (control plane + nodes across the region's zones) with **Workload
  Identity** enabled (`<project>.svc.id.goog`) and a single vanilla node pool (`e2-standard-8`,
  provisional pending load sizing), on `GKE_METADATA` so pods can mint Workload Identity tokens.
  GKE ships CoreDNS, the GCE ingress controller, and a default StorageClass in-cluster, so there
  are no addons to declare.
- A least-privilege **node service account** (logging/monitoring/Artifact Registry reader only) in
  place of the broad Compute Engine default SA. Pod-level GCS access is **not** on the node SA — it
  comes from Workload Identity.
- **Optional** (`enable_storage_identity`, default on) storage + Workload Identity sub-module: the
  **seven** Nexus data-path GCS buckets, one **GSA** every blob-accessing service account
  impersonates, `roles/storage.objectAdmin` on each bucket, and a `roles/iam.workloadIdentityUser`
  binding per Kubernetes SA subject. The seven names derive from a single stem (`blob_prefix`, plus
  a random suffix for GCS global uniqueness): `<stem>-db` (the whole DB data plane shares this one
  bucket — its seven logical stores write disjoint keys) plus six `<stem>-nexus-*` (`source`,
  `knowledge`, `archive`, `traces`, `snapshots`, `library`), one per nexus store. The suffix set is
  a fixed product contract — the operator supplies only the stem.
- **Optional** (`create_ssd_storage_class`, default off) a non-default `nexus-ssd` pd-ssd
  StorageClass for pods that name it. GKE's built-in `standard-rwo` remains the default that
  FoundationDB's unnamed PVCs bind to.

## Networking — sizing the pod range

GKE is **VPC-native**: every pod gets a routable alias IP out of the subnet's **pod secondary
range**, and GKE allocates a **`/24` of that range per node** (110 pods/node by default). So the
**pod range**, not the node range, is what caps density — a small pod range exhausts as soon as the
node count climbs, long before the node range does.

This module sizes the node range at `/20` (4094 nodes — never the bottleneck) and the **pod range at
`/16`** (65 536 IPs = 256 `/24` node blocks), which clears real pod density with wide headroom to
raise the node count or run multiple instances. Services take a separate `/20`. This mirrors the EKS
module's pod-IP planning, where the VPC-CNI likewise draws pod IPs from a range that must hold every
pod, not just every node. The three ranges are independent, non-overlapping blocks; adjust
`subnet_cidr` / `pods_cidr` / `services_cidr` to fit an existing address plan.

## Object storage — bucket-per-store

pc-blob maps each logical store to a whole bucket — there is no bucket+prefix mode — so the layout
is **one DB bucket + six nexus buckets** (seven total), all named from one stem: `<prefix>-db`
(the DB shares this across its seven logical stores, whose keys are disjoint) and
`<prefix>-nexus-<store>` for each nexus store. Buckets use **uniform bucket-level access**, so the
Workload Identity IAM grant is the only thing that governs access.

How the chart consumes these:

- pc-blob selects its driver from **`nexus.config.cloud.provider = "gcp"`**. Credentials are
  **Workload Identity only** — the GCS driver reaches Application Default Credentials, which under
  GKE resolve to the GSA. No account, key, or region field appears in the storage values (GCS
  addresses buckets by their globally-unique name).
- The chart derives the seven bucket names from **`blob.gcs.bucketPrefix`** and annotates every
  blob-accessing service account with `iam.gke.io/gcp-service-account` from **`blob.gcs.serviceAccount`**,
  so neither the per-store names nor the annotation are hand-listed.

`install/gen-values.py` renders these into `values.gcs.yaml` from `customer.yaml`. This module
outputs the **bucket prefix**, the **GSA email**, and the **project**.

## Prerequisites

- GCP credentials in the environment — Application Default Credentials
  (`gcloud auth application-default login`) or `GOOGLE_OAUTH_ACCESS_TOKEN` — for a principal with
  rights to create GKE/VPC/IAM/GCS in the project; `terraform >= 1.5`.
- The **GKE**, **Compute**, and **IAM** APIs enabled on the project.
- Quota for the chosen machine type in the region.

## Usage

```bash
gcloud auth application-default login          # or export GOOGLE_OAUTH_ACCESS_TOKEN
cp terraform.tfvars.example terraform.tfvars   # set project/region, adjust as needed
terraform init
terraform plan
terraform apply
```

Then fetch credentials and confirm the vanilla shape:

```bash
$(terraform output -raw get_credentials_command)
kubectl get nodes
# expect: NO nexus-role labels present
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.labels}{"\n"}{end}' | grep -i nexus-role || echo "OK: no nexus-role labels"
# and no nexus-role taints
kubectl get nodes -o jsonpath='{range .items[*]}{.spec.taints}{"\n"}{end}' | grep -i nexus-role || echo "OK: no nexus-role taints"
```

Validate a workload without ingress via `kubectl port-forward svc/nexus-gateway 80:80`
(set `enable_ingress = true` to reserve a global external IP a GCE Ingress can adopt — the Ingress
object and a `ManagedCertificate` are a separate step).

## Wiring into the chart

`install/tf-to-inputs.sh ../terraform/gke-slim` fills the storage inputs from these outputs and
`gen-values.py` renders them into `values.gcs.yaml`. The mapping:

| Output | Chart value |
|---|---|
| `bucket_prefix` | `blob.gcs.bucketPrefix` (the seven bucket names derive from it) |
| `gsa_email` | `blob.gcs.serviceAccount` — annotated onto every blob-accessing SA |
| `project` | `blob.gcs.project` (with `nexus.config.cloud.provider = gcp`) |

Workload Identity binding is wired **automatically**. The module binds every blob-accessing service
account the umbrella chart runs — the two `nexus-*` SAs (named after `helm_release_name`) plus the
five `db-slim` SAs — in `helm_namespace`, both defaulting to `nexus`. The full list lives in
`locals.tf` (`blob_accessing_service_accounts`). For a standard install you set nothing:

- Running under a different release name / namespace? Set `helm_release_name` / `helm_namespace`
  and the bound set follows.
- Need an extra (non-standard) service account bound? Add it via `workload_service_accounts` —
  entries are appended to the derived set.

## You may stand it up

GKE plus a Cloud NAT and external IPs cost real money. This module is safe to `terraform apply` for
a validation loop, but **always `terraform destroy` when done** — never leave a cluster running.
