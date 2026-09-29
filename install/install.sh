#!/usr/bin/env bash
# Install wrapper — preflight -> render check -> secrets -> helm install|upgrade of the
# published OCI chart, from the overlays gen-values.py emits.
#
# --dry-run runs preflight + lint + render and prints the full plan, creating no
# secrets. =client (default) is offline (helm template); =server validates the
# manifest against the cluster API (catches server-side rejections; needs kube access).
#
# --upgrade re-applies the generated values to the existing release, reusing (never
# minting) the release credentials. Its dry-run is always server-side, and a real
# upgrade runs one first.
#
# Re-runnable: the two generated release credentials are persisted (0600) to
# install/.secrets.env on first run and reused, so re-installs keep stable creds.
#
# Usage:
#   ./install.sh [--upgrade] [--dry-run[=client|server]] [-f customer.yaml] [--yes] [--debug]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

DRY_RUN=0
DRY_RUN_MODE=""         # client = offline helm template; server = validate against the cluster API
UPGRADE=0
ASSUME_YES=0
DEBUG=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --dry-run=*) DRY_RUN=1; DRY_RUN_MODE="${1#*=}" ;;
    --upgrade) UPGRADE=1 ;;
    -f|--inputs) INPUTS_FILE="$2"; shift ;;
    --yes|-y) ASSUME_YES=1 ;;
    --debug) DEBUG=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
case "$DRY_RUN_MODE" in ""|client|server) ;; *) die "--dry-run mode must be 'client' or 'server', got '$DRY_RUN_MODE'" ;; esac
if [ "$UPGRADE" = 1 ]; then
  [ "$DRY_RUN_MODE" != "client" ] || die "--upgrade --dry-run is server-side (it compares against the live release); drop '=client'"
  DRY_RUN_MODE="server"
  HELM_VERB="upgrade"
  HELM_APPLY_ARGS=(--wait)
else
  DRY_RUN_MODE="${DRY_RUN_MODE:-client}"
  HELM_VERB="install"
  HELM_APPLY_ARGS=()
fi

# Pass --debug through to helm only when asked (helm --debug is a firehose).
DEBUG_ARGS=()
[ "$DEBUG" = 1 ] && DEBUG_ARGS=(--debug)

need helm
need python3
[ "$DRY_RUN_MODE" = "server" ] && need kubectl

# --- 1. (re)generate overlays + inputs.env, then preflight -------------------
log "generating overlays from $INPUTS_FILE"
python3 "$HERE/gen-values.py" -f "$INPUTS_FILE" -o "$GEN_DIR"
load_inputs_env

PREFLIGHT_ARGS=()
if [ "$UPGRADE" = 1 ]; then
  log "running static + upgrade preflight"
  PREFLIGHT_ARGS=(--upgrade)
else
  log "running static preflight"
fi
python3 "$HERE/preflight.py" -f "$INPUTS_FILE" "${PREFLIGHT_ARGS[@]}" || die "preflight failed — fix the inputs above and re-run"

CHART_REF="oci://$REGISTRY_BASE/nexus-installer"
VERSION_ARGS=(--version "$CHART_VERSION")
log "chart: $CHART_REF --version $CHART_VERSION"

OVERLAYS=(
  -f "$GEN_DIR/values.install.yaml"
  -f "$GEN_DIR/$STORAGE_VALUES"
  -f "$GEN_DIR/values.self-hosted.yaml"
)
HELM_KUBE=(helm --kube-context "$KUBE_CONTEXT")

# --- 2. every secret the run needs must be in the environment ----------------
# An upgrade re-sends every provider key, so its dry-run checks them too.
require_secret_envs() {
  local names=("$REGISTRY_PASSWORD_ENV") missing=() n
  [ "$STORAGE_AUTH" = "shared_key" ] && names+=("$STORAGE_KEY_ENV")
  [ "${GATEWAY_COVERS_RERANK:-0}" = 1 ] || names+=("$RERANK_KEY_ENV")
  if [ "${GATEWAY_ENABLED:-0}" = 1 ]; then
    names+=("$GATEWAY_CLIENT_ID_ENV" "$GATEWAY_CLIENT_SECRET_ENV")
    [ -n "${GATEWAY_SUBSCRIPTION_KEY_REF:-}" ] && names+=("$GATEWAY_SUBSCRIPTION_KEY_ENV")
  else
    names+=("$LLM_KEY_ENV" "$EMBEDDING_KEY_ENV")
  fi
  for n in "${names[@]}"; do
    [ -n "$n" ] && [ -z "${!n-}" ] && missing+=("$n")
  done
  [ ${#missing[@]} -eq 0 ] || die "these environment variables hold required secrets and are not set: ${missing[*]}"
  log "all ${#names[@]} secret-holding environment variables are set"
}
if [ "$UPGRADE" = 1 ] || [ "$DRY_RUN" = 0 ]; then
  require_secret_envs
fi

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

# Rotating either credential logs every user out, so an upgrade only ever compares the
# local copy against the release (recovering it from there when absent), never mints.
load_or_recover_creds() {
  local origin="" live mode="compare" recovered
  if [ -n "${NEXUS_JWT_SECRET:-}" ] && [ -n "${NEXUS_SESSION_CREDENTIAL:-}" ]; then
    origin="the environment"
  elif [ -f "$SECRETS_ENV" ]; then
    # shellcheck disable=SC1090
    source "$SECRETS_ENV"
    origin="$SECRETS_ENV"
  else
    mode="recover"
  fi
  live="$("${HELM_KUBE[@]}" get values "$RELEASE" -n "$NAMESPACE" -o json 2>/dev/null)" \
    || die "could not read the release values (helm get values $RELEASE -n $NAMESPACE)"
  recovered="$(printf '%s\0%s\0%s' "$live" "${NEXUS_JWT_SECRET:-}" "${NEXUS_SESSION_CREDENTIAL:-}" | python3 -c '
import json, shlex, sys

mode = sys.argv[1]
live_json, jwt, session = sys.stdin.buffer.read().split(b"\0")
live = json.loads(live_json or b"null") or {}
live_jwt = str(((live.get("nexus") or {}).get("auth") or {}).get("jwtSecret") or "")
live_session = str(((live.get("nexus") or {}).get("config") or {}).get("byocSessionCredential") or "")
if not (live_jwt and live_session):
    sys.exit("the release values carry no jwtSecret/byocSessionCredential to reuse; export NEXUS_JWT_SECRET and NEXUS_SESSION_CREDENTIAL to the values the release runs with")
if mode == "recover":
    sys.stdout.write(f"NEXUS_JWT_SECRET={shlex.quote(live_jwt)}\nNEXUS_SESSION_CREDENTIAL={shlex.quote(live_session)}\n")
elif jwt.decode() != live_jwt or session.decode() != live_session:
    sys.exit(3)
' "$mode")" || {
    rc=$?
    [ "$rc" = 3 ] && die "the release credentials in $origin differ from the ones release '$RELEASE' runs with — an upgrade with them would rotate the login credential and invalidate every session. Remove the stale copy (or export the running values) and re-run."
    die "could not reuse the release credentials (see above)"
  }
  if [ "$mode" = "recover" ]; then
    ( umask 177; printf '%s\n' "$recovered" > "$SECRETS_ENV" )
    # shellcheck disable=SC1090
    source "$SECRETS_ENV"
    log "no credentials in the environment or $SECRETS_ENV — recovered the release's own into $SECRETS_ENV (0600); nothing was rotated"
  else
    log "release credentials from $origin match the running release"
  fi
}

if [ "$UPGRADE" = 1 ]; then
  load_or_recover_creds
elif [ "$DRY_RUN" = 0 ]; then
  load_or_make_creds
fi

# --- 4. build the secret values files ----------------------------------------
# Secrets travel in a values file, not --set: helm's strvals parser silently mangles
# any value containing , = { } or \ (`ab,cd=` truncates to `ab`) and empties "null",
# all at exit 0. It must be the LAST -f of every helm call, because the generated
# values.self-hosted.yaml declares the same providerKeys as empty stubs and -f
# precedence is last-wins.
# Every step before the apply (render, lint, server dry-run) gets placeholders, so no real
# secret reaches the render files $GEN_DIR keeps.
PLACEHOLDER_VALUES_FILE=""
SECRET_VALUES_FILE=""
trap 'rm -f "${OUT_FILE:-}" "${PLACEHOLDER_VALUES_FILE:-}" "${SECRET_VALUES_FILE:-}"' EXIT

SECRET_MODE=placeholder
secret_or_placeholder() {
  if [ "$SECRET_MODE" = placeholder ]; then printf 'dryrun-placeholder'; else secret_from_env "$1"; fi
}

write_secret_values_file() {
  local jwt session rerank
  SECRET_MODE="$1"
  if [ "$SECRET_MODE" = placeholder ]; then
    jwt="dryrun-placeholder"
    session="dryrun-placeholder"
  else
    jwt="$NEXUS_JWT_SECRET"
    session="$NEXUS_SESSION_CREDENTIAL"
  fi
  local pairs=(
    nexus/auth/jwtSecret "$jwt"
    nexus/config/byocSessionCredential "$session"
  )
  # Skip the static rerank key when the gateway fronts rerank (catalog uses credential_ref).
  if [ "${GATEWAY_COVERS_RERANK:-0}" != 1 ]; then
    rerank="$(secret_or_placeholder "$RERANK_KEY_ENV")"
    pairs+=( "nexus/inference/providerKeys/$RERANK_KEY_REF" "$rerank" )
  fi
  if [ "${GATEWAY_ENABLED:-0}" = 1 ]; then
    # The chat/embedding credential is a token the proxy mints per refresh window;
    # what gets injected here is the long-lived OAuth2 client behind it.
    local client_id client_secret subscription_key
    client_id="$(secret_or_placeholder "$GATEWAY_CLIENT_ID_ENV")"
    client_secret="$(secret_or_placeholder "$GATEWAY_CLIENT_SECRET_ENV")"
    pairs+=(
      "nexus/inference/providerKeys/$GATEWAY_CLIENT_ID_REF" "$client_id"
      "nexus/inference/providerKeys/$GATEWAY_CLIENT_SECRET_REF" "$client_secret"
    )
    if [ -n "${GATEWAY_SUBSCRIPTION_KEY_REF:-}" ]; then
      subscription_key="$(secret_or_placeholder "$GATEWAY_SUBSCRIPTION_KEY_ENV")"
      pairs+=( "nexus/inference/providerKeys/$GATEWAY_SUBSCRIPTION_KEY_REF" "$subscription_key" )
    fi
  else
    local llm embed entry ref env_name
    llm="$(secret_or_placeholder "$LLM_KEY_ENV")"
    embed="$(secret_or_placeholder "$EMBEDDING_KEY_ENV")"
    pairs+=(
      "nexus/inference/providerKeys/$LLM_KEY_REF" "$llm"
      "nexus/inference/providerKeys/$EMBED_KEY_REF" "$embed"
    )
    # A chat tier naming its own key env gets its own ref (ref:ENV_NAME, space-separated).
    for entry in ${EXTRA_KEY_PAIRS:-}; do
      ref="${entry%%:*}"
      env_name="${entry#*:}"
      pairs+=( "nexus/inference/providerKeys/$ref" "$(secret_or_placeholder "$env_name")" )
    done
  fi
  OUT_FILE="$(mktemp)"
  chmod 600 "$OUT_FILE"
  # JSON is valid YAML, and json.dump is the only serializer here that cannot
  # misquote a value. Values reach python on stdin so they never appear in argv.
  printf '%s\0' "${pairs[@]}" | python3 -c '
import json, sys

fields = sys.stdin.buffer.read().split(b"\0")[:-1]
values = {}
for path, value in zip(fields[::2], fields[1::2]):
    parts = path.decode().split("/")
    node = values
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value.decode()
json.dump(values, sys.stdout)
' > "$OUT_FILE"
}

write_secret_values_file placeholder
PLACEHOLDER_VALUES_FILE="$OUT_FILE"

# --- 5. render the resolved chart and check the data plane it would run -------
RENDER="$GEN_DIR/render.yaml"
log "rendering $CHART_REF $CHART_VERSION"
helm template "${DEBUG_ARGS[@]}" "$RELEASE" "$CHART_REF" "${VERSION_ARGS[@]}" \
  -n "$NAMESPACE" "${OVERLAYS[@]}" -f "$PLACEHOLDER_VALUES_FILE" > "$RENDER" \
  || die "could not render $CHART_REF --version $CHART_VERSION (need 'helm registry login $REGISTRY_SERVER'? is the bundle mirrored?)"
python3 - "$RENDER" "$STATIC_INDEX_ID" "$EMBED_DIMENSION" "$CHART_VERSION" "$HERE" <<'PY' || die "the bundle cannot serve the requested static index (see above)"
import json
import sys

import yaml

render, want_id, want_dim, chart, here = sys.argv[1:6]
sys.path.insert(0, here)
from preflight import deployment_env, index_identity  # noqa: E402

# The data plane and the Nexus side take the index by separate routes, so a chart that
# reads one of them from somewhere else splits the install without failing it.
got_id = got_dim = meta = None
with open(render, encoding="utf-8") as f:
    for doc in yaml.safe_load_all(f):
        if not doc:
            continue
        name = (doc.get("metadata") or {}).get("name", "")
        if doc.get("kind") == "Deployment" and name == "docs-api":
            found_id, found_dim = index_identity(deployment_env(doc))
            if found_id or found_dim:
                got_id, got_dim = found_id, found_dim
        elif doc.get("kind") == "ConfigMap" and name.endswith("-index-metadata"):
            try:
                meta = json.loads((doc.get("data") or {}).get("metadata.json", ""))
            except (TypeError, ValueError):
                meta = None

if got_id is None and got_dim is None:
    sys.exit(f"render of {chart} has no docs-api Deployment carrying an index id")
if got_id != want_id or str(got_dim) != str(want_dim):
    sys.exit(
        f"bundle {chart} renders the data plane at index id {got_id} / dimension {got_dim}, "
        f"but your inputs set staticIndex.id={want_id} / embedding.dimension={want_dim}. "
        "This bundle does not carry your static index; use a bundle Pinecone confirms for it."
    )
if not isinstance(meta, dict):
    sys.exit(
        f"render of {chart} has no readable *-index-metadata ConfigMap. The Nexus services take "
        "the static index from it, so this bundle would run them on a different index than the "
        "data plane; use a bundle Pinecone confirms for your inputs."
    )
meta_id, meta_dim = meta.get("index_id"), meta.get("dimension")
if meta_id != want_id or str(meta_dim) != str(want_dim):
    sys.exit(
        f"bundle {chart} splits the static index: the data plane renders at id {got_id} / "
        f"dimension {got_dim} and your inputs set staticIndex.id={want_id} / "
        f"embedding.dimension={want_dim}, but the Nexus index-metadata ConfigMap carries id "
        f"{meta_id} / dimension {meta_dim}. Use a bundle Pinecone confirms for your inputs."
    )
print(f"render carries the requested static index: id {got_id}, dimension {got_dim}")
PY
log "rendered -> $RENDER ($(grep -c '^kind:' "$RENDER") objects)"

render_images() {
  grep -E '^\s*image:' "$RENDER" | sed -E 's/^\s*image:\s*//; s/^"(.*)"$/\1/' | sort -u
}

image_delta() {
  local live changed
  live="$(kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get pods \
    -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.image}{"\n"}{end}{end}' | sort -u)"
  changed="$(comm -23 <(render_images) <(printf '%s\n' "$live"))"
  if [ -n "$changed" ]; then
    log "images the upgrade rolls to (not running today):"
    printf '%s\n' "$changed" | sed 's/^/    /' >&2
  else
    log "every image in the render is already running — configuration-only change"
  fi
}

server_dry_run() {
  # The API server can only validate namespaced objects against an existing namespace, so
  # ensure the target namespace exists. A --dry-run=server persists nothing else.
  kubectl --context "$KUBE_CONTEXT" create namespace "$NAMESPACE" --dry-run=client -o yaml \
    | kubectl --context "$KUBE_CONTEXT" apply -f - >/dev/null
  "${HELM_KUBE[@]}" "$HELM_VERB" "${DEBUG_ARGS[@]}" "$RELEASE" "$CHART_REF" "${VERSION_ARGS[@]}" \
    -n "$NAMESPACE" "${OVERLAYS[@]}" -f "$PLACEHOLDER_VALUES_FILE" --dry-run=server > "$GEN_DIR/render.server.yaml"
  log "server-side dry-run accepted by the cluster API ($KUBE_CONTEXT) -> $GEN_DIR/render.server.yaml"
}

# --- 6a. dry-run: lint + (server) validate; print the plan ---------------------
if [ "$DRY_RUN" = 1 ]; then
  log "DRY RUN ($DRY_RUN_MODE) — lint + render; no secrets created. client is offline; server validates against the cluster API."

  # helm lint wants a chart path, not an oci:// ref, so pull the OCI chart to a temp dir.
  log "helm lint"
  LINT_DIR="$(mktemp -d)"
  if helm pull "$CHART_REF" "${VERSION_ARGS[@]}" --untar --untardir "$LINT_DIR" 2>/dev/null; then
    helm lint "${DEBUG_ARGS[@]}" "$LINT_DIR/nexus-installer" "${OVERLAYS[@]}" -f "$PLACEHOLDER_VALUES_FILE" >&2 || warn "helm lint reported issues (above)"
  else
    warn "could not pull the chart to lint (need 'helm registry login $REGISTRY_SERVER'?); skipping lint"
  fi
  rm -rf "$LINT_DIR"

  if [ "$DRY_RUN_MODE" = "server" ]; then
    log "server-side dry-run — validating the manifest against the cluster API ($KUBE_CONTEXT)"
    server_dry_run
  fi
  log "images referenced (each must be on your registry base '$REGISTRY_BASE'):"
  render_images | sed 's/^/    /' >&2
  [ "$UPGRADE" = 1 ] && image_delta
  cat >&2 <<EOF

[install] Plan (nothing was applied):
    action    : helm $HELM_VERB
    namespace : $NAMESPACE
    release   : $RELEASE
    chart     : $CHART_REF ${VERSION_ARGS[*]:-}
    overlays  : values.install.yaml, $STORAGE_VALUES, values.self-hosted.yaml
    secrets   : (real run) pull=$PULL_SECRET_NAME, storage=$STORAGE_EXISTING_SECRET, + provider keys via a temp values file (0600, deleted on exit)
EOF
  log "dry-run complete."
  exit 0
fi

# --- 6b. real run: secrets, then helm install|upgrade -------------------------
if [ "$UPGRADE" = 1 ]; then
  image_delta
  server_dry_run
fi

if [ "$ASSUME_YES" != 1 ]; then
  if [ "$UPGRADE" = 1 ]; then
    printf '[install] About to upgrade release "%s" in namespace "%s" on context "%s" to bundle %s. The API is unavailable for about two minutes while the pods roll. Continue? [y/N] ' \
      "$RELEASE" "$NAMESPACE" "$KUBE_CONTEXT" "$BUNDLE_TAG" >&2
  else
    printf '[install] About to create secrets and install release "%s" into namespace "%s" on context "%s". Continue? [y/N] ' \
      "$RELEASE" "$NAMESPACE" "$KUBE_CONTEXT" >&2
  fi
  read -r reply; [ "$reply" = "y" ] || [ "$reply" = "Y" ] || die "aborted"
fi

write_secret_values_file real
SECRET_VALUES_FILE="$OUT_FILE"

log "creating secrets"
"$HERE/create-secrets.sh"

if [ "$UPGRADE" = 1 ]; then
  log "helm upgrade --wait (patient foreground; returns when the rolled pods are Ready, up to 10m)"
else
  log "helm $HELM_VERB (patient foreground; do NOT Ctrl-C while it waits on the verify hook)"
fi
"${HELM_KUBE[@]}" "$HELM_VERB" "${DEBUG_ARGS[@]}" "$RELEASE" "$CHART_REF" "${VERSION_ARGS[@]}" \
  -n "$NAMESPACE" "${OVERLAYS[@]}" -f "$SECRET_VALUES_FILE" --timeout 10m "${HELM_APPLY_ARGS[@]}"

log "$HELM_VERB submitted. Verify: kubectl --context $KUBE_CONTEXT -n $NAMESPACE get pods"
if [ "$UPGRADE" = 1 ]; then
  log "confirm the rolled images: kubectl --context $KUBE_CONTEXT -n $NAMESPACE get pods -o jsonpath='{range .items[*]}{.spec.containers[*].image}{\"\\n\"}{end}' | sort -u"
  log "then exercise the release: ./smoke-test.sh"
fi
