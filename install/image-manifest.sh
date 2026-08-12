#!/usr/bin/env bash
# Image + chart manifest — print the exact artifacts the install pulls so you can
# confirm they are present in your registry first. This does NOT copy anything;
# getting images into your registry is your pipeline's job (an active copy, or a
# pull-through remote that caches on first pull). preflight.py --live checks the
# manifest this writes (generated/manifest.txt).
#
# Usage: ./image-manifest.sh [--list] [--chart-path DIR]
#   --chart-path renders the chart for exact source+dest refs; without it a static
#   fallback keyed off the bundle tags is printed and flagged approximate.
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

# Fallback image set when no chart render is available (approximate — see header).
NEXUS_IMAGES=(nexus_api nexus_orchestrator nexus_runtime nexus_gateway nexus_console \
              nexus_mcp nexus_auth nexus_inference_proxy nexus_file_proxy)
DB_IMAGES=(docs-api index-builder query-routers query-executors-slab request-log-writers)

# "SOURCE_REF<TAB><name>:<tag>" per image, resolved against sourceRegistry — the one
# repo the whole bundle is promoted into. A render gives exact tags; else a static
# fallback, approximate.
source_refs() {
  if [ -n "$CHART_PATH" ] && command -v helm >/dev/null 2>&1; then
    local rendered
    rendered=$(helm template nexus "$CHART_PATH" \
      --set global.image.registry="$SOURCE_REGISTRY" \
      --set nexus.auth.jwtSecret=x --set nexus.config.byocSessionCredential=x 2>/dev/null)
    {
      printf '%s\n' "$rendered" | grep -oE 'image: "?[^"]+' | sed -E 's/^image: "?//'
      # the task runtime is pulled by the orchestrator at task time, so it lands in
      # config rather than a pod image: field — grep it out separately.
      printf '%s\n' "$rendered" | grep -oE '[A-Za-z0-9._/-]+/nexus_runtime:[A-Za-z0-9._-]+'
    } | while IFS= read -r ref; do [ -n "$ref" ] && printf '%s\t%s\n' "$ref" "${ref##*/}"; done | sort -u
    return
  fi
  for i in "${NEXUS_IMAGES[@]}"; do printf '%s/%s:%s\t%s:%s\n' "$SOURCE_REGISTRY" "$i" "$BUNDLE_TAG" "$i" "$BUNDLE_TAG"; done
  for i in "${DB_IMAGES[@]}";    do printf '%s/%s:%s\t%s:%s\n' "$SOURCE_REGISTRY" "$i" "$DB_TAG"    "$i" "$DB_TAG"; done
  printf '%s/%s:%s\t%s:%s\n' "$SOURCE_REGISTRY" foundationdb "$FDB_TAG" foundationdb "$FDB_TAG"
}

MANIFEST="$GEN_DIR/manifest.txt"
: > "$MANIFEST"

echo "# Artifacts the install pulls (dest base = registry.base = $REGISTRY_BASE):"
[ -n "$CHART_PATH" ] || warn "no --chart-path: sources approximate and the nexus image tag may be wrong; pass --chart-path for exact refs."

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
