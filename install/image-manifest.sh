#!/usr/bin/env bash
# Print — and optionally mirror — the images + OCI chart the install pulls. `--list`
# (default) writes generated/manifest.txt (which preflight.py --live verifies) and copies
# nothing. `--copy` stages the whole bundle FROM the --source registry INTO your
# registry.base, so the auth + create-repo + crane-copy loop you'd otherwise hand-write is
# one flag.
#
# The image list, with tags, comes from the chart render: `helm template` is the source of
# truth for what the install pulls. With --chart-path DIR it renders that local checkout;
# without it, it pulls the OCI chart from your own registry (registry.base) into a temp dir
# and renders that — the same chart install.sh pulls, so no route to Pinecone is needed
# (`helm registry login <registry.base host>` first if you are not already logged in).
#
# By default the output lists the DEST refs (<registry.base>/<name>:<tag>) you must have
# present — "confirm these are staged". `--source REGISTRY` adds the "copy FROM" column.
#
# --copy mirrors the whole bundle FROM --source INTO registry.base (--source required).
# Engine auto-detects crane|skopeo; --engine to force. Log in to both registries first —
# this tool never reads a key:
#     gcloud auth print-access-token | crane auth login <source-host> -u oauth2accesstoken --password-stdin
#     aws ecr get-login-password     | crane auth login <registry.base host> -u AWS --password-stdin
#
# Usage: ./image-manifest.sh [--list] [--copy] [--engine crane|skopeo] [--chart-path DIR] [--source REGISTRY]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

CHART_PATH=""
SOURCE=""
COPY=0
ENGINE=""   # empty = auto-detect (crane, else skopeo); --engine overrides
while [ $# -gt 0 ]; do
  case "$1" in
    --list) : ;;
    --copy) COPY=1 ;;
    --engine) ENGINE="$2"; shift ;;
    --chart-path) CHART_PATH="$2"; shift ;;
    --source)
      { [ $# -ge 2 ] && [ "${2#-}" = "$2" ]; } || die "--source needs a <registry> argument"
      SOURCE="$2"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

if [ "$COPY" -eq 1 ] && [ -z "$SOURCE" ]; then
  die "--copy requires --source <registry>"
fi

load_inputs_env
need helm

# Cap a network-touching command so an unreachable registry or a stalled auth helper
# ends with a diagnosable timeout instead of hanging the run. PULL_TIMEOUT (seconds)
# raises the cap for a slow link. Uses gtimeout where coreutils is prefixed (macOS);
# runs unbounded only if neither timeout binary is installed.
PULL_TIMEOUT="${PULL_TIMEOUT:-180}"
COPY_TIMEOUT="${COPY_TIMEOUT:-600}"
bounded() {
  local t="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$t" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$t" "$@"
  else "$@"; fi
}
run_bounded() { bounded "$PULL_TIMEOUT" "$@"; }

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
# A copy runs inside an `if`, where bash swallows SIGINT; trap it so Ctrl+C stops the run
# instead of falling through to the next artifact.
trap 'echo >&2; exit 130' INT TERM

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

# Read loop, not mapfile, to stay bash 3.2-safe (macOS). The chart artifact rides along.
REFS=()
while IFS= read -r nt; do [ -n "$nt" ] && REFS+=("$nt"); done < <(render_refs)
REFS+=("nexus-installer:$CHART_VERSION")

for nt in "${REFS[@]}"; do
  emit "$REGISTRY_BASE/$nt" "$nt"
done

if [ -n "$SOURCE" ]; then
  echo
  echo "# Upstream SOURCE repo these publish from (mirror-operator informational — the whole"
  echo "# bundle is promoted into one repo, so a pull-through remote fronting it resolves all):"
  render_refs | sed -E "s#^#$SOURCE/#; s#/[^/]+:[^/]+\$##" | sort -u | sed 's/^/  /'
fi
echo
echo "# wrote $MANIFEST  (preflight.py --live verifies it)"

# --- mirror (--copy) ---
ecr_region_from_host() { printf '%s\n' "$1" | sed -nE 's/^[^.]+\.dkr\.ecr\.([^.]+)\.amazonaws\.com$/\1/p'; }

ensure_dest_repo() {
  # ECR has no push-time repo autocreate; make it if absent. Other registries no-op.
  local host="$1" repo="$2" region
  case "$host" in
    *.dkr.ecr.*.amazonaws.com)
      command -v aws >/dev/null 2>&1 || {
        warn "aws not found — skipping ECR repo autocreate; crane copy will fail if a repo is absent"; return 0; }
      region="$(ecr_region_from_host "$host")"
      [ -n "$region" ] || { warn "could not parse an ECR region from $host — skipping repo autocreate"; return 0; }
      if aws ecr describe-repositories --region "$region" --repository-names "$repo" >/dev/null 2>&1; then
        return 0
      fi
      log "creating ECR repository $repo ($region)"
      aws ecr create-repository --region "$region" --repository-name "$repo" >/dev/null 2>&1 \
        || warn "could not create ECR repository $repo — assuming it exists or you lack ecr:CreateRepository"
      ;;
  esac
  return 0
}

resolve_engine() {
  case "$ENGINE" in
    crane|skopeo) need "$ENGINE"; return ;;
    "") : ;;
    *) die "unknown --engine '$ENGINE' (want: crane | skopeo)" ;;
  esac
  if command -v crane >/dev/null 2>&1; then ENGINE=crane
  elif command -v skopeo >/dev/null 2>&1; then ENGINE=skopeo
  else die "no image-copy engine found — install crane (github.com/google/go-containerregistry) \
or skopeo (github.com/containers/skopeo), or pass --engine"; fi
}

# skopeo needs the docker:// transport prefix; crane takes the bare ref.
copy_ref() {
  case "$ENGINE" in
    crane)  bounded "$COPY_TIMEOUT" crane  copy "$1" "$2" ;;
    # --all copies the whole manifest list; without it skopeo copies only the host platform
    # and fails on a Mac for these linux images (crane copies the full index by default).
    skopeo) bounded "$COPY_TIMEOUT" skopeo copy --all "docker://$1" "docker://$2" ;;
  esac
}

mirror_bundle() {
  resolve_engine
  local host="${REGISTRY_BASE%%/*}"
  local base_path=""; case "$REGISTRY_BASE" in */*) base_path="${REGISTRY_BASE#*/}" ;; esac
  local total="${#REFS[@]}" n=0 name repo login="$ENGINE login"
  [ "$ENGINE" = crane ] && login="crane auth login"
  echo
  log "mirroring $total artifacts: $SOURCE  ->  $REGISTRY_BASE  ($ENGINE copy; idempotent)"
  for nt in "${REFS[@]}"; do
    n=$((n + 1))
    name="${nt%%:*}"
    if [ -n "$base_path" ]; then repo="$base_path/$name"; else repo="$name"; fi
    ensure_dest_repo "$host" "$repo"
    log "[$n/$total] $nt"
    copy_ref "$SOURCE/$nt" "$REGISTRY_BASE/$nt" && continue
    # Stop at the first failure instead of repeating it 15 more times: it is almost always
    # a missing login, and re-running is idempotent so nothing is lost.
    warn "copy FAILED: $nt (error above)"
    die "stopped at the first failure — usually $ENGINE is not logged in. Log in to BOTH, then re-run:
      $login ${SOURCE%%/*}
      $login $host
    ACR: 'az acr login' updates Docker's keychain — crane reads it, skopeo does not; with skopeo run '$login $host'."
  done
  log "mirror complete: $total/$total artifacts staged in $REGISTRY_BASE"
  log "verify: python3 $HERE/preflight.py --live"
}

[ "$COPY" -eq 1 ] && mirror_bundle
