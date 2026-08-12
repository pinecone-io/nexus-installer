#!/usr/bin/env bash
# Print the images + OCI chart the install pulls, so you can confirm they are staged
# in your registry (this copies nothing — staging is your pipeline's job). Writes
# generated/manifest.txt, which preflight.py --live verifies.
#
# Usage: ./image-manifest.sh [--list] [--chart-path DIR]
#   --chart-path renders the chart for exact refs; otherwise a static, approximate list.
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

NEXUS_IMAGES=(nexus_api nexus_orchestrator nexus_runtime nexus_gateway nexus_console \
              nexus_mcp nexus_auth nexus_inference_proxy nexus_file_proxy)
DB_IMAGES=(docs-api index-builder query-routers query-executors-slab request-log-writers)

source_refs() {
  if [ -n "$CHART_PATH" ] && command -v helm >/dev/null 2>&1; then
    local rendered
    rendered=$(helm template nexus "$CHART_PATH" \
      --set global.image.registry="$SOURCE_REGISTRY" \
      --set nexus.auth.jwtSecret=x --set nexus.config.byocSessionCredential=x 2>/dev/null)
    {
      printf '%s\n' "$rendered" | grep -oE 'image: "?[^"]+' | sed -E 's/^image: "?//'
      # runtime image is pulled at task time, so it lands in config, not a pod image:
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
