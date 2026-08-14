#!/usr/bin/env bash
# Print the images + OCI chart the install pulls, so you can confirm they are staged
# in your registry (this copies nothing — staging is your pipeline's job). Writes
# generated/manifest.txt, which preflight.py --live verifies.
#
# The image list, with tags, comes from the chart render: `helm template` is the source of
# truth for what the install pulls. With --chart-path DIR it renders that local checkout;
# without it, it pulls the OCI chart from your own registry (registry.base) into a temp dir
# and renders that — the same chart install.sh pulls, so no route to Pinecone is needed
# (`helm registry login <registry.base host>` first if you are not already logged in).
#
# By default the output lists the DEST refs (<registry.base>/<name>:<tag>) you must have
# present — the customer's need is "confirm these are staged". Mirroring the bundle FROM
# Pinecone is a separate, operator-only step: pass --source [REGISTRY] to add the "copy
# FROM" column (bare --source uses Pinecone's distribution registry).
#
# Usage: ./image-manifest.sh [--list] [--chart-path DIR] [--source [REGISTRY]]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# Pinecone's distribution registry — the only meaningful --source for the mirror step.
# Used only when --source is passed with no explicit registry.
PINECONE_SOURCE="us-docker.pkg.dev/pinecone-artifacts/nexus"

CHART_PATH=""
SOURCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list) : ;;
    --chart-path) CHART_PATH="$2"; shift ;;
    # Optional registry argument: a bare --source defaults to Pinecone's source.
    --source)
      if [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; then SOURCE="$2"; shift; else SOURCE="$PINECONE_SOURCE"; fi ;;
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

# The chart is pulled from registry.base — the same single artifact install.sh installs
# from — so a customer never needs a route to Pinecone's registry. helm authenticates to
# an OCI registry with its own stored credential, not the cloud CLI's. A Google Artifact
# Registry host (*.pkg.dev / *.gcr.io) takes a short-lived access token piped into `helm
# registry login`; other registries take a plain login.
DEST_HOST="${REGISTRY_BASE%%/*}"
case "$DEST_HOST" in
  *.pkg.dev|*.gcr.io|gcr.io)
    LOGIN_HINT="gcloud auth print-access-token | helm registry login $DEST_HOST -u oauth2accesstoken --password-stdin" ;;
  *)
    LOGIN_HINT="helm registry login $DEST_HOST" ;;
esac

PULL_TMP=""
# return 0 so an empty PULL_TMP (the --chart-path case) does not become the exit status.
cleanup() { [ -n "$PULL_TMP" ] && rm -rf "$PULL_TMP"; return 0; }
trap cleanup EXIT

if [ -z "$CHART_PATH" ]; then
  PULL_TMP="$(mktemp -d)"
  err="$PULL_TMP/pull.err"
  log "pulling OCI chart oci://$REGISTRY_BASE/nexus-installer:$CHART_VERSION to derive the image list…"
  # Do not close helm's stdin: an authenticated OCI pull against GAR hangs under </dev/null
  # though a bare pull is instant. run_bounded is the hang guard instead. stderr is captured
  # so the failure reason surfaces in the message below.
  rc=0
  run_bounded helm pull "oci://$REGISTRY_BASE/nexus-installer" --version "$CHART_VERSION" \
    --untar --untardir "$PULL_TMP" 2>"$err" || rc=$?
  if [ "$rc" -ne 0 ]; then
    timed_out=""
    [ "$rc" -eq 124 ] && timed_out="
      (timed out after ${PULL_TIMEOUT}s — likely no route to $DEST_HOST; raise PULL_TIMEOUT if the link is just slow)"
    die "could not pull the OCI chart oci://$REGISTRY_BASE/nexus-installer --version $CHART_VERSION:
$(sed 's/^/      /' "$err" 2>/dev/null)$timed_out
    The image list is derived from the chart render, so a reachable chart is required. Either:
      - log helm in to your registry (helm uses its own credential, not the cloud CLI's):
          $LOGIN_HINT
      - ensure the bundle (chart included) is mirrored into $REGISTRY_BASE first
      - or pass a local chart checkout:  $0 --list --chart-path DIR"
  fi
  CHART_PATH="$PULL_TMP/nexus-installer"
fi
[ -d "$CHART_PATH" ] || die "--chart-path not a directory: $CHART_PATH"

# Render once, up front: both the dest list and the upstream-repo summary read from it,
# and templating a multi-subchart bundle is the expensive step. Render against registry.base
# so image refs already carry the dest prefix.
RENDERED=$(helm template nexus "$CHART_PATH" \
  --set global.image.registry="$REGISTRY_BASE" \
  --set nexus.auth.jwtSecret=x --set nexus.config.byocSessionCredential=x 2>/dev/null)

# Every image ref (incl. the FoundationDB server image) surfaces as an `image:` field
# in the render; the runtime image is pulled at task time, so it lands in config instead.
# Emit just <name>:<tag> — the dest prefix is registry.base, the source prefix (if asked
# for) is --source, so the bare name+tag is all the caller needs.
render_refs() {
  {
    printf '%s\n' "$RENDERED" | grep -oE 'image: "?[^"]+' | sed -E 's/^image: "?//'
    printf '%s\n' "$RENDERED" | grep -oE '[A-Za-z0-9._/-]+/nexus_runtime:[A-Za-z0-9._-]+'
  } | while IFS= read -r ref; do [ -n "$ref" ] && printf '%s\n' "${ref##*/}"; done | sort -u
}

MANIFEST="$GEN_DIR/manifest.txt"
: > "$MANIFEST"

echo "# Artifacts that must be present in registry.base ($REGISTRY_BASE):"
[ -n "$SOURCE" ] && echo "# (--source $SOURCE: '<- FROM' column is the mirror-copy source, operator-only)"

emit() {
  # $1 = dest ref (also the manifest entry); $2 = name:tag for the FROM column
  if [ -n "$SOURCE" ]; then
    printf '  %-64s  <- %s\n' "$1" "$SOURCE/$2"
  else
    printf '  %s\n' "$1"
  fi
  echo "$1" >> "$MANIFEST"
}

while IFS= read -r nt; do
  emit "$REGISTRY_BASE/$nt" "$nt"
done < <(render_refs)

emit "$REGISTRY_BASE/nexus-installer:$CHART_VERSION" "nexus-installer:$CHART_VERSION"

if [ -n "$SOURCE" ]; then
  echo
  echo "# Upstream SOURCE repo these publish from (mirror-operator informational — the whole"
  echo "# bundle is promoted into one repo, so a pull-through remote fronting it resolves all):"
  render_refs | sed -E "s#^#$SOURCE/#; s#/[^/]+:[^/]+\$##" | sort -u | sed 's/^/  /'
fi
echo
echo "# wrote $MANIFEST  (preflight.py --live verifies it)"
