# terraform/eks-slim — optional turnkey EKS cluster (slim install)

Stands up a **deliberately vanilla** EKS cluster for a self-hosted Nexus **slim** install. It
looks like a customer's own Kubernetes cluster so the install exercises the real path — in
particular the **empty-nodeSelector** scheduling behaviour: there are **no `nexus-role` node
labels or taints** and no dedicated Nexus node groups, so a regression there surfaces here instead
of hiding.

**Optional by design.** A customer bringing their own cluster (and/or their own bucket + IAM role)
skips this — it is not a dependency of the chart. It is the executable form of the self-hosted
prerequisites.

**Optional and decoupled.** This module is not a dependency of the chart or the install scripts;
`install/tf-to-inputs.sh` reads its outputs into `customer.yaml` when you use it. See
[object storage](#object-storage--bucket-per-store) for the layout it provisions.

## Naming

Resources are named `<type>-<name_prefix>-<environment>` — e.g. `eks-nexus-slim-dev`,
`vpc-eks-nexus-slim-dev`. `slim` is the deployment config; `environment` (default `dev`) is a
per-instance knob, so the same module stands up `staging`/`prod` instances without editing names.
Individual names can still be overridden (`cluster_name`, `bucket_name`, ...).

## What it creates

- VPC, an internet gateway, one NAT gateway, and — across `az_count` (>= 2) AZs — a `/20` private
  subnet per AZ (node + pod IPs) plus a `/24` public subnet per AZ (NAT egress, optional ALB).
  See [networking](#networking--sizing-the-node-subnet) for why the private subnet is large.
- EKS: single managed node group (2× `m6i.2xlarge`, provisional pending load sizing) across the
  private subnets, with the `vpc-cni`, `kube-proxy`, `coredns`, and `aws-ebs-csi-driver` addons.
  `vpc-cni` runs with prefix delegation on by default. API-mode access with the creator
  bootstrapped as cluster-admin.
- A **default `gp3` StorageClass** backed by the EBS CSI driver. EKS ships no working default
  class (the legacy in-tree `gp2` provisioner is gone in current Kubernetes), so without this
  stateful pods stay `Pending` on unbound PVCs.
- Cluster and node IAM roles, the **IAM OIDC provider** registered from the cluster's issuer (the
  trust anchor IRSA needs), and an IRSA role for the EBS CSI controller (managed nodes' IMDS hop
  limit of 1 blocks the controller from borrowing node credentials, so it gets its own).
- **Optional** (`enable_storage_identity`, default on) storage + IRSA sub-module: the **seven**
  Nexus data-path S3 buckets, a single IRSA role trusted by every blob-accessing service account,
  and its S3 policy scoped to those buckets. The seven names derive from a single stem
  (`blob_prefix`, plus a random suffix for S3 global uniqueness): `<stem>-db` (the whole DB data
  plane shares this one bucket — its seven logical stores write disjoint keys) plus six
  `<stem>-nexus-*` (`source`, `knowledge`, `archive`, `traces`, `snapshots`, `library`), one per
  nexus store. The suffix set is a fixed product contract — the operator supplies only the stem.

## Networking — sizing the node subnet

The AWS VPC CNI assigns every pod a routable IP out of the node subnet, so the subnet must hold
every pod, not just every node. A small subnet exhausts as soon as pod density climbs.

A full workload across 3 AZs was measured to fit inside a `/20` VPC carved into `/22` private
subnets (~1019 IPs each), peaking at ~350 pods / ~960 consumed IPs per AZ — so a `/20` VPC is about
the floor for real pod density. This module is more generous: a `/16` VPC with a **`/20` private
subnet per AZ** (4094 IPs), which clears that floor and leaves room to raise `az_count` or run
multiple instances. `enable_prefix_delegation` (on by default) hands each ENI a `/28` prefix
instead of single secondary IPs, lifting pods-per-node further.

## Object storage — bucket-per-store

pc-blob maps each logical store to a whole bucket — there is no bucket+prefix mode — so the layout
is **one DB bucket + six nexus buckets** (seven total), all named from one stem: `<prefix>-db`
(the DB shares this across its seven logical stores, whose keys are disjoint) and
`<prefix>-nexus-<store>` for each nexus store.

How the chart consumes these:

- pc-blob selects its driver from **`nexus.config.cloud.provider = "aws"`** (plus `cloud.region`).
  Credentials are **IRSA only** — no account or key field appears in the storage values.
- The chart derives the seven bucket names from **`blob.s3.bucketPrefix`** and annotates every
  blob-accessing service account with `eks.amazonaws.com/role-arn` from **`blob.s3.roleArn`**, so
  neither the per-store names nor the annotation are hand-listed.

`install/gen-values.py` renders these into `values.s3.yaml` from `customer.yaml`. This module
outputs the **bucket prefix**, the **region**, and the **IRSA role ARN**.

## Prerequisites

- AWS credentials in the environment (`AWS_PROFILE` or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`)
  with rights to create VPC/EKS/IAM/S3; `terraform >= 1.5`.
- Service quota for the chosen instance type in the region.
- A region offering at least `az_count` AZs (all commercial regions do for the default of 2).

## Usage

```bash
export AWS_PROFILE=<your-profile>            # or export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
cp terraform.tfvars.example terraform.tfvars # adjust as needed
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
(set `enable_load_balancer_controller = true` to stage the AWS Load Balancer Controller IRSA role +
policy — the controller Helm install itself is a separate step).

## Wiring into the chart

`install/tf-to-inputs.sh` fills the storage inputs from these outputs and `gen-values.py` renders
them into `values.s3.yaml`. The mapping:

| Output | Chart value |
|---|---|
| `bucket_prefix` | `blob.s3.bucketPrefix` (the seven bucket names derive from it) |
| `irsa_role_arn` | `blob.s3.roleArn` — annotated onto every blob-accessing SA |
| `region` | `blob.s3.region` / `nexus.config.cloud.region` (with `cloud.provider = aws`) |

IRSA trust is wired **automatically**. The module trusts every blob-accessing service account the
umbrella chart runs — the two `nexus-*` SAs (named after `helm_release_name`) plus the five
`db-slim` SAs — in `helm_namespace`, both defaulting to `nexus`. The full list lives in `locals.tf`
(`blob_accessing_service_accounts`). For a standard install you set nothing:

- Running under a different release name / namespace? Set `helm_release_name` / `helm_namespace`
  and the trusted set follows.
- Need an extra (non-standard) service account trusted? Add it via `workload_service_accounts` —
  entries are appended to the derived set.

## You may stand it up

EKS plus a NAT gateway costs real money. This module is safe to `terraform apply` for a validation
loop, but **always `terraform destroy` when done** — never leave a cluster running.
