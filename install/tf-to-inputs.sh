#!/usr/bin/env bash
# Terraform hand-off — emit the storage/identity half of customer.yaml from a slim
# cluster module's Terraform outputs, so on a greenfield cluster those inputs are filled
# automatically. Prints a YAML fragment to stdout; paste it under your customer.yaml
# (or redirect and merge).
#
# The provider is detected from which outputs the module emits:
#   aks-slim (Azure) -> blob_storage_account / blob_container / workload_identity_client_id
#   eks-slim (AWS)   -> db_bucket / nexus_buckets / irsa_role_arn / region
#   gke-slim (GCP)   -> db_bucket / nexus_buckets / gsa_email / project
#
# Usage:
#   ./tf-to-inputs.sh [TF_DIR]      # default TF_DIR = ../terraform/aks-slim
#                                   #   AWS: ./tf-to-inputs.sh ../terraform/eks-slim
#                                   #   GCP: ./tf-to-inputs.sh ../terraform/gke-slim
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

TF_DIR="${1:-$HERE/../terraform/aks-slim}"
need terraform
need python3
[ -d "$TF_DIR" ] || die "terraform dir not found: $TF_DIR"

log "reading terraform outputs from $TF_DIR"
# Pass the JSON through the environment, not a pipe: `python3 - <<'PY'` reads its program
# from stdin, so a piped `terraform output` would be shadowed by the heredoc and lost.
TF_OUTPUT_JSON="$(terraform -chdir="$TF_DIR" output -json)" python3 - <<'PY'
import json, os, sys

o = json.loads(os.environ["TF_OUTPUT_JSON"])
def val(k):
    v = o.get(k, {}).get("value")
    return v if v not in (None, "") else None

cluster = val("cluster_name")
# gke-slim uses a dedicated kube_context output (get-credentials writes gke_<proj>_<region>_<name>);
# eks-slim aliases the context to the cluster name.
kube_context = val("kube_context") or cluster

# gke-slim (GCP / gcs) is distinguished by gsa_email; eks-slim (AWS / s3) by bucket_prefix
# alone; aks-slim (Azure / abs) by the account. Check GCP first — it also emits bucket_prefix.
bucket_prefix = val("bucket_prefix")
gsa_email = val("gsa_email")
account = val("blob_storage_account")

if gsa_email is not None:
    project = val("project")
    print("# --- generated from gke-slim terraform outputs; merge into customer.yaml ---")
    if kube_context:
        print(f"kubeContext: {kube_context}")
    print("storage:")
    print("  provider: gcs")
    if bucket_prefix: print(f"  bucketPrefix: {bucket_prefix}")
    print(f"  serviceAccount: {gsa_email}")
    if project: print(f"  project: {project}")
    sys.exit(0)

if bucket_prefix is not None:
    region = val("region")
    role_arn = val("irsa_role_arn")
    print("# --- generated from eks-slim terraform outputs; merge into customer.yaml ---")
    if cluster:
        # get_credentials_command sets the kube context to this alias.
        print(f"kubeContext: {cluster}")
    print("storage:")
    print("  provider: s3")
    print(f"  bucketPrefix: {bucket_prefix}")
    if region:   print(f"  region: {region}")
    if role_arn: print(f"  roleArn: {role_arn}")
    sys.exit(0)

if account is None and val("workload_identity_client_id") is None:
    sys.stderr.write(
        "tf-to-inputs: no storage/identity outputs found "
        "(enable_storage_identity = false, or unrecognized module). Nothing to emit.\n")
    sys.exit(1)

prefix = val("blob_container")
client = val("workload_identity_client_id")
print("# --- generated from aks-slim terraform outputs; merge into customer.yaml ---")
if cluster:
    print(f"kubeContext: {cluster}")
print("storage:")
print("  provider: abs")
if account: print(f"  account: {account}")
if prefix:  print(f"  containerPrefix: {prefix}")
# The module wires keyless workload-identity federation, so default that half here.
print("  auth: workload_identity")
if client: print(f"  clientId: {client}")
PY
