#!/usr/bin/env bash
# Image + chart mirror helper — copy the image bundle and the OCI chart from
# Pinecone's source registry into your own registry (ACR / Artifactory), keyed off
# the bundle tag. One command over the bundle manifest.
#
# The authoritative image list is DERIVED from the chart render (the chart's own
# `image:` refs), so it can never drift from what the install actually pulls. A
# static fallback list is used when a render is not available.
#
# Copy engine: skopeo if present, else `az acr import` (server-side, no local pull).
#
# Usage:
#   ./mirror.sh --list                 # print the image + chart set for the bundle tag
#   ./mirror.sh --dry-run              # print every copy command, run nothing
#   ./mirror.sh [--chart-path DIR]     # perform the copies (source -> your base)
#
# --chart-path renders a local chart to derive the list offline; without it the list
# comes from the static fallback (the OCI source chart cannot be templated without
# first pulling it, which is what we are about to mirror).
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

MODE=copy         # copy | list | dry-run
CHART_PATH=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list) MODE=list ;;
    --dry-run) MODE=dry-run ;;
    --chart-path) CHART_PATH="$2"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

load_inputs_env

# Nexus images the chart deploys. Fallback when no render is available.
NEXUS_IMAGES=(nexus_api nexus_orchestrator nexus_runtime nexus_gateway nexus_console \
              nexus_mcp nexus_auth nexus_inference_proxy nexus_file_proxy)
DB_IMAGES=(docs-api index-builder query-routers query-executors-slab request-log-writers)

image_list() {
  if [ -n "$CHART_PATH" ] && command -v helm >/dev/null 2>&1; then
    # Render with a source registry so the refs carry the flat <name>:<tag>, then
    # strip the registry to recover name:tag pairs.
    helm template nexus "$CHART_PATH" \
      --set global.image.registry="$SOURCE_REGISTRY" \
      --set nexus.auth.jwtSecret=x --set nexus.config.byocSessionCredential=x 2>/dev/null \
      | grep -oE "$SOURCE_REGISTRY/[A-Za-z0-9_./-]+:[A-Za-z0-9_.-]+" \
      | sed "s#^$SOURCE_REGISTRY/##" | sort -u
    return
  fi
  for i in "${NEXUS_IMAGES[@]}"; do echo "$i:$BUNDLE_TAG"; done
  for i in "${DB_IMAGES[@]}";    do echo "$i:$DB_TAG"; done
  echo "foundationdb:$FDB_TAG"
}

CHART_ARTIFACT="nexus-installer:$CHART_VERSION"

if [ "$MODE" = list ]; then
  echo "# images (source: $SOURCE_REGISTRY, dest: $REGISTRY_BASE):"
  image_list | sed 's/^/  /'
  echo "# OCI chart:"
  echo "  $CHART_ARTIFACT"
  cat <<EOF
# Known public-registry gap: a few auxiliary images are not under the
# registry override (bitnami/bitnamisecure kubectl helpers, registry.k8s.io/pause:3.9,
# alpine/k8s). In a no-egress posture, allow those pulls or preload them on the nodes.
EOF
  exit 0
fi

if command -v skopeo >/dev/null 2>&1; then
  ENGINE=skopeo
elif command -v az >/dev/null 2>&1; then
  ENGINE=acr
  ACR_NAME="${REGISTRY_SERVER%%.*}"
else
  die "need skopeo or az to mirror"
fi
log "mirror engine: $ENGINE"

copy_image() {
  local nt="$1" src="$SOURCE_REGISTRY/$1" dst="$REGISTRY_BASE/$1"
  if [ "$MODE" = dry-run ]; then
    if [ "$ENGINE" = skopeo ]; then
      echo "skopeo copy --all docker://$src docker://$dst"
    else
      echo "az acr import -n $ACR_NAME --source $src --image ${REGISTRY_BASE#*/}/$nt"
    fi
    return
  fi
  log "copy $nt"
  if [ "$ENGINE" = skopeo ]; then
    skopeo copy --all "docker://$src" "docker://$dst"
  else
    az acr import -n "$ACR_NAME" --source "$src" --image "${REGISTRY_BASE#*/}/$nt" --force
  fi
}

copy_chart() {
  local src="$SOURCE_REGISTRY/$CHART_ARTIFACT" dst="$REGISTRY_BASE/$CHART_ARTIFACT"
  if [ "$MODE" = dry-run ]; then
    if [ "$ENGINE" = skopeo ]; then
      echo "skopeo copy docker://$src docker://$dst   # OCI chart artifact"
    else
      echo "az acr import -n $ACR_NAME --source $src --image ${REGISTRY_BASE#*/}/$CHART_ARTIFACT"
    fi
    return
  fi
  log "copy chart $CHART_ARTIFACT"
  if [ "$ENGINE" = skopeo ]; then
    skopeo copy "docker://$src" "docker://$dst"
  else
    az acr import -n "$ACR_NAME" --source "$src" --image "${REGISTRY_BASE#*/}/$CHART_ARTIFACT" --force
  fi
}

while IFS= read -r nt; do copy_image "$nt"; done < <(image_list)
copy_chart
[ "$MODE" = dry-run ] && log "dry-run: nothing copied." || log "mirror complete."
