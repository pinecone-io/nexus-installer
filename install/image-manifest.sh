#!/usr/bin/env bash
# Print the images + OCI chart the install pulls, so you can confirm they are staged
# in your registry (this copies nothing — staging is your pipeline's job). Writes
# generated/manifest.txt, which preflight.py --live verifies.
#
# The image list, with tags, comes from the chart render: `helm template` is the source of
# truth for what the install pulls. With --chart-path DIR it renders that local checkout;
# without it, it pulls the OCI chart from your sourceRegistry into a temp dir and renders
# that (needs `helm registry login` first).
#
# Usage: ./image-manifest.sh [--list] [--chart-path DIR]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

CHART_PATH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list) : ;;
    --chart-path) CHART_PATH="$2"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

load_inputs_env
need helm

# Cap a network-touching command so an unreachable registry or a stalled auth helper
# ends with a diagnosable timeout instead of hanging the run. PULL_TIMEOUT (seconds)
# raises the cap for a slow link. Uses gtimeout where coreutils is prefixed (macOS);
# runs unbounded only if neither timeout binary is installed.
PULL_TIMEOUT="${PULL_TIMEOUT:-180}"
run_bounded() {
  if command -v timeout >/dev/null 2>&1; then timeout "$PULL_TIMEOUT" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$PULL_TIMEOUT" "$@"
  else "$@"; fi
}

# helm authenticates to an OCI registry with its own stored credential, not the cloud
# CLI's — so refreshing a cloud login is not enough. A Google Artifact Registry host
# (*.pkg.dev / *.gcr.io) takes a short-lived access token piped into `helm registry
# login`; other registries take a plain login.
SRC_HOST="${SOURCE_REGISTRY%%/*}"
case "$SRC_HOST" in
  *.pkg.dev|*.gcr.io|gcr.io)
    LOGIN_HINT="gcloud auth print-access-token | helm registry login $SRC_HOST -u oauth2accesstoken --password-stdin" ;;
  *)
    LOGIN_HINT="helm registry login $SRC_HOST" ;;
esac

PULL_TMP=""
cleanup() { [ -n "$PULL_TMP" ] && rm -rf "$PULL_TMP"; }
trap cleanup EXIT

if [ -z "$CHART_PATH" ]; then
  PULL_TMP="$(mktemp -d)"
  err="$PULL_TMP/pull.err"
  log "pulling OCI chart oci://$SOURCE_REGISTRY/nexus-installer:$CHART_VERSION to derive the image list…"
  # Do not close helm's stdin: an authenticated OCI pull against GAR hangs under </dev/null
  # though a bare pull is instant. run_bounded is the hang guard instead. stderr is captured
  # so the failure reason surfaces in the message below.
  rc=0
  run_bounded helm pull "oci://$SOURCE_REGISTRY/nexus-installer" --version "$CHART_VERSION" \
    --untar --untardir "$PULL_TMP" 2>"$err" || rc=$?
  if [ "$rc" -ne 0 ]; then
    timed_out=""
    [ "$rc" -eq 124 ] && timed_out="
      (timed out after ${PULL_TIMEOUT}s — likely no route to ${SOURCE_REGISTRY%%/*}; raise PULL_TIMEOUT if the link is just slow)"
    die "could not pull the OCI chart oci://$SOURCE_REGISTRY/nexus-installer --version $CHART_VERSION:
$(sed 's/^/      /' "$err" 2>/dev/null)$timed_out
    The image list is derived from the chart render, so a reachable chart is required. Either:
      - log helm in to the source registry (helm uses its own credential, not the cloud CLI's):
          $LOGIN_HINT
      - or pass a local chart checkout:  $0 --list --chart-path DIR"
  fi
  CHART_PATH="$PULL_TMP/nexus-installer"
fi
[ -d "$CHART_PATH" ] || die "--chart-path not a directory: $CHART_PATH"

# Render once, up front: both the dest list and the upstream-repo summary read from it,
# and templating a multi-subchart bundle is the expensive step.
RENDERED=$(helm template nexus "$CHART_PATH" \
  --set global.image.registry="$SOURCE_REGISTRY" \
  --set nexus.auth.jwtSecret=x --set nexus.config.byocSessionCredential=x 2>/dev/null)

# Every image ref (incl. the FoundationDB server image) surfaces as an `image:` field
# in the render; the runtime image is pulled at task time, so it lands in config instead.
source_refs() {
  {
    printf '%s\n' "$RENDERED" | grep -oE 'image: "?[^"]+' | sed -E 's/^image: "?//'
    printf '%s\n' "$RENDERED" | grep -oE '[A-Za-z0-9._/-]+/nexus_runtime:[A-Za-z0-9._-]+'
  } | while IFS= read -r ref; do [ -n "$ref" ] && printf '%s\t%s\n' "$ref" "${ref##*/}"; done | sort -u
}

MANIFEST="$GEN_DIR/manifest.txt"
: > "$MANIFEST"

echo "# Artifacts the install pulls (dest base = registry.base = $REGISTRY_BASE):"

while IFS=$'\t' read -r src nt; do
  name="${nt%:*}"; tag="${nt##*:}"
  dest="$REGISTRY_BASE/$name:$tag"
  printf '  %-64s  <- %s\n' "$dest" "$src"
  echo "$dest" >> "$MANIFEST"
done < <(source_refs)

chart_dest="$REGISTRY_BASE/nexus-installer:$CHART_VERSION"
printf '  %-64s  <- %s\n' "$chart_dest" "$SOURCE_REGISTRY/nexus-installer:$CHART_VERSION"
echo "$chart_dest" >> "$MANIFEST"

echo
echo "# Upstream SOURCE repo these publish from (informational — the whole bundle is"
echo "# promoted into this one repo, so a pull-through remote fronting it resolves all):"
source_refs | cut -f1 | sed -E 's#/[^/]+:[^/]+$##' | sort -u | sed 's/^/  /'
echo
echo "# wrote $MANIFEST  (preflight.py --live verifies it)"
