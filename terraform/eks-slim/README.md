# terraform/eks-slim — optional turnkey EKS cluster (slim install)

Stands up a **deliberately vanilla** EKS cluster for a self-hosted Nexus **slim** install. It
looks like a customer's own Kubernetes cluster so the install exercises the real path — in
particular the **empty-nodeSelector** scheduling behaviour: there are **no `nexus-role` node
labels or taints** and no dedicated Nexus node groups, so a regression there surfaces here instead
of hiding.

**Optional by design.** A customer bringing their own cluster (and/or their own bucket + IAM role)
skips this — it is not a dependency of the chart. It is the executable form of the self-hosted
prerequisites.

**Terraform only.** This module is decoupled from the install scripts and the chart. Consuming its
outputs from `install/` (an S3 branch in `gen-values.py`, etc.) is a separate task — see
[object storage](#object-storage--bucket-per-store-read-this) below.

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
  stateful pods stay `Pending` on unbound PVCs; AKS provides one out of the box and this matches it.
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

A full Nexus cell across 3 AZs fits inside a `/20` VPC carved into `/22` private subnets (~1019 IPs
each), peaking at ~350 pods / ~960 consumed IPs per AZ with roughly 2× headroom still free — so a
`/20` VPC is the floor for a real cell, and the cell wizard's CIDR validator rejects anything
smaller ("too few pod IPs for the EKS VPC CNI"). This module is more generous: a `/16` VPC with a
**`/20` private subnet per AZ** (4094 IPs), which clears that floor and leaves room to raise
`az_count` or run multiple instances. `enable_prefix_delegation` (on by default) hands each ENI a
`/28` prefix instead of single secondary IPs, lifting pods-per-node further.

## Object storage — bucket-per-store (read this)

This mirrors the Azure layout one-for-one: **one DB bucket + six nexus buckets** (seven total),
the S3 equivalent of the seven Azure containers-from-one-stem. Pinecone's blob layer maps each
logical store to a whole bucket (there is no bucket+prefix mode), so bucket-per-store is the
faithful translation — the DB shares one bucket across its seven logical stores (their keys are
disjoint), and each nexus store gets its own.

Two facts about how the chart consumes this, confirmed against the chart templates:

- pc-blob selects its driver from **`nexus.config.cloud.provider = "aws"`** (plus `cloud.region`).
  Credentials are **IRSA only** — the chart annotates each service account with
  `eks.amazonaws.com/role-arn` (via the pass-through `serviceAccountAnnotations` value); the AWS
  SDK default credential chain picks up the web-identity token. There is **no** account/key/
  `clientId`/`roleArn` field inside the storage values.
- The nexus half already takes explicit per-store bucket names (`config.storage.{source,knowledge,
  archive,traces,snapshots,library}`) and emits them driver-neutrally, so it needs no chart change.
  The DB half's blob-env template is Azure-only (`abs-blob-env.gotmpl`); an AWS slim install needs
  the S3 sibling that points its seven stores at `db_bucket`. **The installer itself emits Azure
  Blob values today** (`install/gen-values.py` has no S3 branch yet) — wiring these outputs in is
  the separate later task.

The module outputs what that path needs: the **DB bucket**, the **six nexus buckets** (by store),
and the **IRSA role ARN**.

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

After apply, feed the outputs into the chart. Because the slim S3 overlay is not implemented in the
installer yet (above), the mapping below is the intended target, not a wired contract:

| Output | Chart value (intended) |
|---|---|
| `db_bucket` | db-slim `PINECONE_BLOB_STORE__*_BUCKET_NAME` (all seven DB stores point here) |
| `nexus_buckets` | `config.storage.{source,knowledge,archive,traces,snapshots,library}` |
| `irsa_role_arn` | each blob-accessing SA's `eks.amazonaws.com/role-arn` annotation |
| `region` | `nexus.config.cloud.region` (with `cloud.provider = aws`) |

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
