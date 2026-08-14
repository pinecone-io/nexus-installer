#!/usr/bin/env bash
# Install wrapper — orchestrates the whole install from the generated overlays:
#
#   preflight (static) -> secrets -> helm install (OCI by default; local-chart
#   fallback for a non-default dimension/index id) with the generated overlays and
#   --set for the generated JWT + session credentials and the model provider keys.
#
# --dry-run runs preflight + lint + render and prints the full plan, creating no
# secrets. =client (default) is offline (helm template); =server validates the
# manifest against the cluster API (catches server-side rejections; needs kube access).
#
# Re-runnable: the two generated release credentials are persisted (0600) to
# install/.secrets.env on first run and reused, so re-installs keep stable creds.
# NOTE: this chart is install-only — to iterate, uninstall + delete PVCs, then
# re-run (see README). Never `helm upgrade` it.
#
# Usage:
#   ./install.sh [--dry-run[=client|server]] [--path auto|oci|local] [--chart-path DIR]
#                [-f customer.yaml] [--yes] [--debug]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

DRY_RUN=0
DRY_RUN_MODE="client"   # client = offline helm template; server = validate against the cluster API
PATH_OVERRIDE="auto"
CHART_PATH=""
ASSUME_YES=0
DEBUG=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --dry-run=*) DRY_RUN=1; DRY_RUN_MODE="${1#*=}" ;;
    --path) PATH_OVERRIDE="$2"; shift ;;
    --chart-path) CHART_PATH="$2"; shift ;;
    -f|--inputs) INPUTS_FILE="$2"; shift ;;
    --yes|-y) ASSUME_YES=1 ;;
    --debug) DEBUG=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
case "$DRY_RUN_MODE" in client|server) ;; *) die "--dry-run mode must be 'client' or 'server', got '$DRY_RUN_MODE'" ;; esac

# Pass --debug through to helm only when asked (helm --debug is a firehose).
DEBUG_ARGS=()
[ "$DEBUG" = 1 ] && DEBUG_ARGS=(--debug)

need helm
need python3

# --- 1. (re)generate overlays + inputs.env, then preflight -------------------
log "generating overlays from $INPUTS_FILE"
python3 "$HERE/gen-values.py" -f "$INPUTS_FILE" -o "$GEN_DIR"
load_inputs_env

log "running static preflight"
python3 "$HERE/preflight.py" -f "$INPUTS_FILE" || die "preflight failed — fix the inputs above and re-run"

# --- 2. resolve the install path (OCI vs local-chart) ------------------------
RESOLVED_PATH="$INSTALL_PATH"   # from inputs.env: oci unless dim/id differ from baked
[ "$PATH_OVERRIDE" != "auto" ] && RESOLVED_PATH="$PATH_OVERRIDE"

if [ "$RESOLVED_PATH" = "oci" ]; then
  CHART_REF="oci://$REGISTRY_BASE/nexus-installer"
  VERSION_ARGS=(--version "$CHART_VERSION")
  log "install path: OCI  ($CHART_REF --version $CHART_VERSION)"
else
  [ -n "$CHART_PATH" ] || die "local-chart path selected (dimension/index id differ from the baked bundle) but --chart-path was not given. Point it at the chart checkout Pinecone provides, run remint-dbslim.sh for your dimension first, then re-run."
  [ -d "$CHART_PATH" ] || die "--chart-path not a directory: $CHART_PATH"
  CHART_REF="$CHART_PATH"
  VERSION_ARGS=()
  log "install path: local-chart  ($CHART_REF)"
fi

OVERLAYS=(
  -f "$GEN_DIR/values.install.yaml"
  -f "$GEN_DIR/$STORAGE_VALUES"
  -f "$GEN_DIR/values.self-hosted.yaml"
)

# --- 3. release credentials (generated JWT + session credential) -------------
# Stable across re-runs so a re-install does not invalidate live sessions.
SECRETS_ENV="$HERE/.secrets.env"
load_or_make_creds() {
  if [ -n "${NEXUS_JWT_SECRET:-}" ] && [ -n "${NEXUS_SESSION_CREDENTIAL:-}" ]; then
    log "using release credentials from the environment"
    return
  fi
  if [ -f "$SECRETS_ENV" ]; then
    # shellcheck disable=SC1090
    source "$SECRETS_ENV"
    log "loaded release credentials from $SECRETS_ENV"
    return
  fi
  need openssl
  NEXUS_JWT_SECRET="$(openssl rand -hex 32)"
  NEXUS_SESSION_CREDENTIAL="$(openssl rand -hex 32)"
  ( umask 177; {
      printf 'NEXUS_JWT_SECRET=%s\n' "$NEXUS_JWT_SECRET"
      printf 'NEXUS_SESSION_CREDENTIAL=%s\n' "$NEXUS_SESSION_CREDENTIAL"
    } > "$SECRETS_ENV" )
  log "generated release credentials -> $SECRETS_ENV (0600). Keep this file safe; the session credential is the API login."
}

# --- 4. build the --set list -------------------------------------------------
# Under dry-run we use placeholder release creds + provider keys (no real secret is
# read). Otherwise resolve creds + provider keys by reference.
build_set_args() {
  local -n out=$1
  if [ "$DRY_RUN" = 1 ]; then
    out=(
      --set "nexus.auth.jwtSecret=dryrun-placeholder"
      --set "nexus.config.byocSessionCredential=dryrun-placeholder"
      --set "nexus.inference.providerKeys.$LLM_KEY_REF=dryrun-placeholder"
      --set "nexus.inference.providerKeys.$EMBED_KEY_REF=dryrun-placeholder"
      --set "nexus.inference.providerKeys.$RERANK_KEY_REF=dryrun-placeholder"
    )
    return
  fi
  load_or_make_creds
  local llm embed rerank
  llm="$(secret_from_env "$LLM_KEY_ENV")"
  embed="$(secret_from_env "$EMBEDDING_KEY_ENV")"
  rerank="$(secret_from_env "$RERANK_KEY_ENV")"
  out=(
    --set "nexus.auth.jwtSecret=$NEXUS_JWT_SECRET"
    --set "nexus.config.byocSessionCredential=$NEXUS_SESSION_CREDENTIAL"
    --set "nexus.inference.providerKeys.$LLM_KEY_REF=$llm"
    --set "nexus.inference.providerKeys.$EMBED_KEY_REF=$embed"
    --set "nexus.inference.providerKeys.$RERANK_KEY_REF=$rerank"
  )
}

SET_ARGS=()
build_set_args SET_ARGS

# --- 5a. dry-run: lint + render (client=template, server=API validation) -----
if [ "$DRY_RUN" = 1 ]; then
  log "DRY RUN ($DRY_RUN_MODE) — lint + render; no secrets created. client is offline; server validates against the cluster API."

  # helm lint wants a chart path, not an oci:// ref, so pull the OCI chart to a temp dir.
  log "helm lint"
  if [ "$RESOLVED_PATH" = "oci" ]; then
    LINT_DIR="$(mktemp -d)"
    if helm pull "$CHART_REF" "${VERSION_ARGS[@]}" --untar --untardir "$LINT_DIR" 2>/dev/null; then
      helm lint "${DEBUG_ARGS[@]}" "$LINT_DIR/nexus-installer" "${OVERLAYS[@]}" "${SET_ARGS[@]}" >&2 || warn "helm lint reported issues (above)"
    else
      warn "could not pull the chart to lint (need 'helm registry login $REGISTRY_SERVER'?); skipping lint"
    fi
    rm -rf "$LINT_DIR"
  else
    helm lint "${DEBUG_ARGS[@]}" "$CHART_REF" "${OVERLAYS[@]}" "${SET_ARGS[@]}" >&2 || warn "helm lint reported issues (above)"
  fi

  RENDER="$GEN_DIR/render.yaml"
  if [ "$DRY_RUN_MODE" = "server" ]; then
    need kubectl
    log "server-side dry-run — validating the manifest against the cluster API ($KUBE_CONTEXT)"
    # The API server can only validate namespaced objects against an existing namespace, so
    # ensure the target namespace exists. A --dry-run=server install persists nothing else.
    kubectl --context "$KUBE_CONTEXT" create namespace "$NAMESPACE" --dry-run=client -o yaml \
      | kubectl --context "$KUBE_CONTEXT" apply -f - >/dev/null
    helm --kube-context "$KUBE_CONTEXT" install "${DEBUG_ARGS[@]}" "$RELEASE" "$CHART_REF" "${VERSION_ARGS[@]}" \
      -n "$NAMESPACE" "${OVERLAYS[@]}" "${SET_ARGS[@]}" --dry-run=server > "$RENDER"
  else
    helm template "${DEBUG_ARGS[@]}" "$RELEASE" "$CHART_REF" "${VERSION_ARGS[@]}" \
      -n "$NAMESPACE" "${OVERLAYS[@]}" "${SET_ARGS[@]}" > "$RENDER"
  fi
  log "rendered -> $RENDER ($(grep -c '^kind:' "$RENDER") objects)"
  log "images referenced (each must be on your registry base '$REGISTRY_BASE'):"
  grep -E '^\s*image:' "$RENDER" | sed 's/^/    /' | sort -u >&2
  cat >&2 <<EOF

[install] Plan (nothing was applied):
    namespace : $NAMESPACE
    release   : $RELEASE
    path      : $RESOLVED_PATH
    chart     : $CHART_REF ${VERSION_ARGS[*]:-}
    overlays  : values.install.yaml, $STORAGE_VALUES, values.self-hosted.yaml
    secrets   : (real run) pull=$PULL_SECRET_NAME, storage=$STORAGE_EXISTING_SECRET, + provider keys via --set
EOF
  log "dry-run complete."
  exit 0
fi

# --- 5b. real install: secrets then helm install -----------------------------
if [ "$ASSUME_YES" != 1 ]; then
  printf '[install] About to create secrets and install release "%s" into namespace "%s" on context "%s". Continue? [y/N] ' \
    "$RELEASE" "$NAMESPACE" "$KUBE_CONTEXT" >&2
  read -r reply; [ "$reply" = "y" ] || [ "$reply" = "Y" ] || die "aborted"
fi

log "creating secrets"
"$HERE/create-secrets.sh"

log "helm install (patient foreground; do NOT Ctrl-C while it waits on the verify hook)"
helm --kube-context "$KUBE_CONTEXT" install "${DEBUG_ARGS[@]}" "$RELEASE" "$CHART_REF" "${VERSION_ARGS[@]}" \
  -n "$NAMESPACE" "${OVERLAYS[@]}" "${SET_ARGS[@]}" --timeout 10m

log "install submitted. Verify: kubectl --context $KUBE_CONTEXT -n $NAMESPACE get pods"
