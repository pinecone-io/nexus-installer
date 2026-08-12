#!/usr/bin/env bash
# Terraform hand-off — emit the storage/identity half of customer.yaml from the
# aks-slim Terraform outputs, so on a greenfield cluster those inputs are filled
# automatically. Prints a YAML fragment to stdout; paste it under your customer.yaml
# (or redirect and merge).
#
# Mapping (terraform/aks-slim/outputs.tf -> customer.yaml):
#   blob_storage_account          -> storage.account
#   blob_container                -> storage.containerPrefix   (a stem, not one container)
#   workload_identity_client_id   -> storage.clientId          (workload_identity auth)
#   cluster_name                  -> kubeContext               (after az aks get-credentials)
#
# Usage:
#   ./tf-to-inputs.sh [TF_DIR]      # default TF_DIR = ../terraform/aks-slim
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

TF_DIR="${1:-$HERE/../terraform/aks-slim}"
need terraform
need python3
[ -d "$TF_DIR" ] || die "terraform dir not found: $TF_DIR"

log "reading terraform outputs from $TF_DIR"
terraform -chdir="$TF_DIR" output -json | python3 - <<'PY'
import json, sys

o = json.load(sys.stdin)
def val(k):
    v = o.get(k, {}).get("value")
    return v if v not in (None, "") else None

account = val("blob_storage_account")
prefix  = val("blob_container")
client  = val("workload_identity_client_id")
cluster = val("cluster_name")

if account is None and client is None:
    sys.stderr.write(
        "tf-to-inputs: storage/identity outputs are null "
        "(enable_storage_identity = false). Nothing to emit.\n")
    sys.exit(1)

print("# --- generated from aks-slim terraform outputs; merge into customer.yaml ---")
if cluster:
    print(f"kubeContext: {cluster}")
print("storage:")
if account: print(f"  account: {account}")
if prefix:  print(f"  containerPrefix: {prefix}")
# The module wires keyless workload-identity federation, so default that half here.
print("  auth: workload_identity")
if client: print(f"  clientId: {client}")
PY
