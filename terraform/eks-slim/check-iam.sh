#!/usr/bin/env bash
# Pre-apply IAM check for the eks-slim module: verify the current AWS identity is
# allowed to perform every action `terraform apply` needs, via the IAM policy
# simulator. Creates nothing.
#
# Advisory, not a guarantee: the simulator evaluates the principal's identity
# policies only. It does not apply SCPs or a permissions boundary unless present,
# and resource-scoped grants are checked against "*", so a PASS is necessary but
# not sufficient. A DENIED line is the actionable signal — grant it before apply.
#
# Usage:
#   ./check-iam.sh [--principal-arn ARN] [--region us-east-1] [--actions-file FILE]
#
# The principal ARN is auto-detected. An assumed-role/SSO session is resolved to
# its underlying role ARN (needs iam:GetRole); pass --principal-arn to skip that.
# The check itself needs iam:SimulatePrincipalPolicy.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="us-east-1"
PRINCIPAL_ARN=""
ACTIONS_FILE="$HERE/required-iam.txt"

die() { echo "error: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --principal-arn) PRINCIPAL_ARN="${2:?--principal-arn needs a value}"; shift 2 ;;
    --region)        REGION="${2:?--region needs a value}"; shift 2 ;;
    --actions-file)  ACTIONS_FILE="${2:?--actions-file needs a value}"; shift 2 ;;
    -h|--help)       grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

command -v aws >/dev/null 2>&1 || die "aws CLI not found"
[ -f "$ACTIONS_FILE" ] || die "actions file not found: $ACTIONS_FILE"

# read loop (not mapfile) to stay bash 3.2-safe on macOS
ACTIONS=()
while IFS= read -r a; do
  [ -n "$a" ] && ACTIONS+=("$a")
done < <(sed -E 's/#.*//' "$ACTIONS_FILE" | tr -s '[:space:]' '\n' | grep -E '^[A-Za-z0-9-]+:[A-Za-z0-9*]+$' | sort -u)
[ "${#ACTIONS[@]}" -gt 0 ] || die "no actions parsed from $ACTIONS_FILE"

if [ -z "$PRINCIPAL_ARN" ]; then
  caller="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)" \
    || die "aws sts get-caller-identity failed — are credentials configured?"
  case "$caller" in
    *:assumed-role/*)
      rolename="$(printf '%s' "$caller" | sed -E 's#.*:assumed-role/([^/]+)/.*#\1#')"
      PRINCIPAL_ARN="$(aws iam get-role --role-name "$rolename" --query 'Role.Arn' --output text 2>/dev/null || true)"
      [ -n "$PRINCIPAL_ARN" ] && [ "$PRINCIPAL_ARN" != "None" ] || die \
"current creds are an assumed role ($rolename) and iam:GetRole is unavailable to resolve its ARN.
    Re-run with the role ARN explicitly:
      $0 --principal-arn arn:aws:iam::<account>:role/$rolename"
      ;;
    *:user/*) PRINCIPAL_ARN="$caller" ;;
    *) die "unrecognized caller ARN '$caller' — pass --principal-arn" ;;
  esac
fi

echo "Checking ${#ACTIONS[@]} actions against: $PRINCIPAL_ARN"
echo

denied=0
checked=0
batch=()

run_batch() {
  [ "${#batch[@]}" -gt 0 ] || return 0
  local out
  out="$(aws iam simulate-principal-policy \
    --policy-source-arn "$PRINCIPAL_ARN" \
    --action-names "${batch[@]}" \
    --query 'EvaluationResults[].[EvalActionName,EvalDecision]' \
    --output text 2>&1)" || die \
"iam:SimulatePrincipalPolicy call failed — the running identity likely lacks that permission.
    Run this check as an identity that has iam:SimulatePrincipalPolicy (and iam:GetRole).
    Underlying error:
$(printf '%s' "$out" | sed 's/^/      /')"
  while IFS=$'\t' read -r action decision; do
    [ -n "$action" ] || continue
    checked=$((checked + 1))
    if [ "$decision" = "allowed" ]; then
      printf '  [ok]     %s\n' "$action"
    else
      printf '  [DENIED] %s (%s)\n' "$action" "$decision"
      denied=$((denied + 1))
    fi
  done <<< "$out"
  batch=()
}

for a in "${ACTIONS[@]}"; do
  batch+=("$a")
  # simulate-principal-policy caps action-names per call; stay well under it
  [ "${#batch[@]}" -ge 50 ] && run_batch
done
run_batch

echo
if [ "$denied" -gt 0 ]; then
  echo "FAIL: ${denied}/${checked} actions denied — grant these before 'terraform apply'."
  exit 1
fi
echo "PASS: all ${checked} actions allowed."
echo "(Advisory: SCPs and permission boundaries are not evaluated; resource-scoped grants checked against '*'.)"
