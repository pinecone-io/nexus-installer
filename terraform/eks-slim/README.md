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
[the S3 contract](#object-storage--the-s3-contract-read-this) below.

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
  private subnets, with the `vpc-cni`, `kube-proxy`, and `coredns` addons. `vpc-cni` runs with
  prefix delegation on by default. API-mode access with the creator bootstrapped as cluster-admin.
- Cluster and node IAM roles, and the **IAM OIDC provider** registered from the cluster's issuer —
  the trust anchor IRSA needs.
- **Optional** (`enable_storage_identity`, default on) storage + IRSA sub-module: an S3 bucket, the
  **seven** Nexus data-path key prefixes, a single IRSA role trusted by every blob-accessing
  service account, and its bucket-scoped S3 policy. The seven prefixes derive from a single stem
  (`blob_prefix`): `<stem>-db` (the whole DB data plane shares this one) plus six `<stem>-nexus-*`
  (`source`, `knowledge`, `archive`, `traces`, `snapshots`, `library`). The suffix set is a fixed
  product contract — the operator supplies only the stem.

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

## Object storage — the S3 contract (read this)

**The installer's slim path emits Azure Blob values today.** `install/gen-values.py` writes the
`blob.provider: abs` / `blob.abs.*` tree and has no S3 branch; there is no `blob.s3` slim schema in
the repo yet. So the exact helm value keys a future AWS slim overlay will use are **not
specified**, and this module cannot mirror keys that do not exist. Rather than guess silently, here
is the assumption this module is built on, drawn from the validated AWS cell path:

- pc-blob selects its driver from **`nexus.config.cloud.provider = "aws"`** (plus `cloud.region`),
  **not** from a `blob.provider` string. On AWS the provider string is `"aws"`, not `"s3"`.
- Credentials are **IRSA only** — the chart annotates each service account with
  `eks.amazonaws.com/role-arn`; the AWS SDK default credential chain then picks up the web-identity
  token. There is **no** account/key/`clientId`/`roleArn` field inside the storage values.
- A live AWS cell uses **separate buckets** (3 Nexus: source/knowledge/archive, plus the DB's own
  buckets). This module instead packs everything into **one bucket with seven key prefixes**
  (`<stem>-db` + six `<stem>-nexus-*`), so the operator supplies a single stem. When the installer
  grows an S3 branch, reconcile the two: either the branch consumes `blob_bucket` + `blob_prefix`
  as a stem, or this module switches to per-bucket outputs. The prefixes need no provisioning on S3
  (a prefix springs into being on first write); the module lays down one zero-byte marker each only
  so the layout is visible via `aws s3 ls`.

The module outputs what any of those contracts needs: the **bucket**, the **prefix stem**, and the
**IRSA role ARN**.

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

After apply, feed the outputs into the chart. Because the slim S3 overlay is not implemented yet
(above), the mapping below is the intended target, not a wired contract:

| Output | Chart value (intended) |
|---|---|
| `blob_bucket` | S3 bucket name |
| `blob_prefix` | key-prefix stem (if the branch derives prefixes from a stem) |
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
