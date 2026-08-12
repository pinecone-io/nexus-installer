#!/usr/bin/env bash
# Shared helpers for the install toolkit. Sourced by the *.sh scripts.
# Rule: never echo a secret. Secrets are read from named env vars and passed to
# tools by reference/stdin only.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN_DIR="${GEN_DIR:-$HERE/generated}"
INPUTS_FILE="${INPUTS_FILE:-$HERE/customer.yaml}"

log()  { printf '\033[36m[install]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33m[install] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[install] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Load generated/inputs.env (KEY=VALUE, no secrets). Run gen-values.py first.
load_inputs_env() {
  local envf="$GEN_DIR/inputs.env"
  [ -f "$envf" ] || die "missing $envf — run: python3 $HERE/gen-values.py -f $INPUTS_FILE"
  # shellcheck disable=SC1090
  set -a; source "$envf"; set +a
}

# Resolve the value of a secret from the env var whose NAME is in $1. Prints the
# value to stdout for capture into a local variable; callers must not log it.
secret_from_env() {
  local var_name="$1"
  [ -n "$var_name" ] || die "no env-var name given for a required secret"
  local val="${!var_name-}"
  [ -n "$val" ] || die "environment variable \$$var_name is not set (holds a required secret)"
  printf '%s' "$val"
}

need() { command -v "$1" >/dev/null 2>&1 || die "required tool not found: $1"; }
