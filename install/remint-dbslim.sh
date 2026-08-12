#!/usr/bin/env bash
# Local-chart helper — re-mint the static index id and/or set a non-default embedding
# dimension in a nexus chart checkout, for the local-chart install path.
#
# WHY THIS EXISTS: the published OCI chart bakes the data-plane index id and dimension
# into the generated db-slim values at build time; the OCI path cannot override them
# (see README "OCI vs local-chart path"). To install at a non-default dimension (e.g.
# 1536 for text-embedding-3-small) or with a freshly minted index id, you install from
# a chart checkout after regenerating its db-slim values for those values. This runs
# the runbook step 2a against that checkout.
#
# Usage:
#   ./remint-dbslim.sh --chart-path DIR [--id UUID] [--dimension N]
#
# Reads the current id/dimension from install/generated/inputs.env when --id/--dimension
# are omitted. Edits chart/scripts/inputs/common.yaml in place, then runs
# scripts/gen-dbslim-values.py to regenerate charts/db-slim/values.yaml.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

CHART_PATH=""; NEW_ID=""; NEW_DIM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --chart-path) CHART_PATH="$2"; shift ;;
    --id) NEW_ID="$2"; shift ;;
    --dimension) NEW_DIM="$2"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

[ -n "$CHART_PATH" ] || die "--chart-path is required (the chart checkout Pinecone provides)"
COMMON="$CHART_PATH/scripts/inputs/common.yaml"
GEN="$CHART_PATH/scripts/gen-dbslim-values.py"
[ -f "$COMMON" ] || die "not a chart checkout: missing $COMMON"
[ -f "$GEN" ] || die "not a chart checkout: missing $GEN"

if [ -z "$NEW_ID" ] || [ -z "$NEW_DIM" ]; then
  [ -f "$GEN_DIR/inputs.env" ] && { set -a; source "$GEN_DIR/inputs.env"; set +a; }
  NEW_ID="${NEW_ID:-${STATIC_INDEX_ID:-}}"
  NEW_DIM="${NEW_DIM:-${EMBED_DIMENSION:-}}"
fi
[ -n "$NEW_ID" ] || die "no index id (pass --id or generate inputs.env first)"
[ -n "$NEW_DIM" ] || die "no dimension (pass --dimension or generate inputs.env first)"

need python3
python3 -c 'import yaml' 2>/dev/null || die "PyYAML required (python3 -m pip install pyyaml)"

DEFAULT_ID="8fe737ee-67aa-47c4-b7b2-ed5ac39c552e"

log "re-minting db-slim inputs in $COMMON: id=$NEW_ID dimension=$NEW_DIM"
sed -i "s/$DEFAULT_ID/$NEW_ID/g" "$COMMON"

# Rewrite the dimension in all three places (env value, and the two dimensions inside
# the PINECONE_HEADLESS__SCHEMA JSON). Done in python to target only dimension fields.
python3 - "$COMMON" "$NEW_DIM" <<'PY'
import re, sys
path, new_dim = sys.argv[1], sys.argv[2]
src = open(path).read()
# PINECONE_HEADLESS__DIMENSION env value (quoted scalar following the name)
src = re.sub(r'(PINECONE_HEADLESS__DIMENSION\s*\n\s*value:\s*)([\'"]?)\d+([\'"]?)',
             lambda m: f'{m.group(1)}{m.group(2)}{new_dim}{m.group(3)}', src)
# "dimension": N inside the schema JSON string
src = re.sub(r'("dimension"\s*:\s*)\d+', lambda m: f'{m.group(1)}{new_dim}', src)
open(path, "w").write(src)
print(f"updated dimension -> {new_dim} in {path}")
PY

log "regenerating charts/db-slim/values.yaml"
python3 "$GEN"
log "done. Install with: ./install.sh --path local --chart-path $CHART_PATH"
