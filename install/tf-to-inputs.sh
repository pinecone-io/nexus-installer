#!/usr/bin/env bash
# Terraform hand-off — emit the storage/identity half of customer.yaml from a slim
# cluster module's Terraform outputs, so on a greenfield cluster those inputs are filled
# automatically. Prints a YAML fragment to stdout; paste it under your customer.yaml
# (or redirect and merge).
#
# The provider is detected from which outputs the module emits:
#   aks-slim (Azure) -> blob_storage_account / blob_container / workload_identity_client_id
#   eks-slim (AWS)   -> db_bucket / nexus_buckets / irsa_role_arn / region
#
# Usage:
#   ./tf-to-inputs.sh [TF_DIR]      # default TF_DIR = ../terraform/aks-slim
#                                   #   AWS: ./tf-to-inputs.sh ../terraform/eks-slim
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

# eks-slim (AWS / s3) is distinguished by bucket_prefix; aks-slim (Azure / abs) by the account.
bucket_prefix = val("bucket_prefix")
account = val("blob_storage_account")

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
