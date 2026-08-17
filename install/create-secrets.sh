#!/usr/bin/env bash
# Create the Kubernetes secrets the install consumes, idempotently, from env-var
# references. Never echoes secret values. Safe to re-run: every object is applied
# via `create ... --dry-run=client -o yaml | kubectl apply`.
#
#   - namespace `nexus`
#   - the registry pull Secret (docker-registry)
#   - the storage-key Secret (shared_key auth only)
#
# Model provider keys are NOT created here — they are injected at install time via
# --set (install.sh); the nexus chart materializes them.
#
# Usage: ./create-secrets.sh                 # applies
#        ./create-secrets.sh --dry-run       # prints what it would do, touches nothing
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

load_inputs_env
need kubectl

KCTX=(kubectl --context "$KUBE_CONTEXT")

apply_stdin() {
  # $1: human label. Reads a manifest on stdin and applies it (or prints under dry-run).
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] would apply: $1"
    cat >/dev/null
  else
    "${KCTX[@]}" apply -f - >/dev/null
    log "applied: $1"
  fi
}

# --- namespace (idempotent) --------------------------------------------------
"${KCTX[@]}" create namespace "$NAMESPACE" --dry-run=client -o yaml | apply_stdin "namespace/$NAMESPACE"

# --- registry pull secret ----------------------------------------------------
REGISTRY_PASSWORD="$(secret_from_env "$REGISTRY_PASSWORD_ENV")"
"${KCTX[@]}" -n "$NAMESPACE" create secret docker-registry "$PULL_SECRET_NAME" \
  --docker-server="$REGISTRY_SERVER" \
  --docker-username="$REGISTRY_USERNAME" \
  --docker-password="$REGISTRY_PASSWORD" \
  --dry-run=client -o yaml | apply_stdin "secret/$PULL_SECRET_NAME (docker-registry)"
unset REGISTRY_PASSWORD

# --- storage-key secret (shared_key only) ------------------------------------
if [ "$STORAGE_AUTH" = "shared_key" ]; then
  [ -n "$STORAGE_EXISTING_SECRET" ] || die "storage.existingSecret is empty under shared_key auth"
  [ -n "$STORAGE_KEY_ENV" ] || die "storage.storageKeyEnv is empty under shared_key auth"
  STORAGE_KEY="$(secret_from_env "$STORAGE_KEY_ENV")"
  "${KCTX[@]}" -n "$NAMESPACE" create secret generic "$STORAGE_EXISTING_SECRET" \
    --from-literal=azure-storage-access-key="$STORAGE_KEY" \
    --dry-run=client -o yaml | apply_stdin "secret/$STORAGE_EXISTING_SECRET (storage key)"
  unset STORAGE_KEY
else
  log "auth=$STORAGE_AUTH — no storage-key Secret needed (keyless blob access)"
fi

log "secrets step complete."
