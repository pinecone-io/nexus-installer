#!/usr/bin/env bash
# Collect a support bundle for a Nexus self-hosted install into a local .tgz.
# Nothing is uploaded — you review the archive and send it to Pinecone yourself.
#
# Collected: cluster and node facts, the namespaced objects the stack uses, pod logs
# (current and previous), namespace events, the Helm release, a static preflight
# re-run, and the generated install inputs. Every file is passed through redact.py
# before the archive is written; the result is listed in REDACTIONS.txt.
#
# Read-only by default — get, describe and logs only. A collector that fails is
# recorded in collection-errors.log and does not stop the run — a partial bundle is
# always produced. The exception is redaction: if that fails, no archive is written.
#
# Usage:
#   ./support-bundle.sh [--since 24h] [--tail 20000] [--exec]
#                       [-f customer.yaml] [-o OUTPUT_DIR]
#
#   --since / --tail  per-container log window; widen when the incident is older
#   --exec            also collect what needs create on a pod subresource: the gateway
#                     version probe (port-forward) and `fdbcli status` (exec)
#   -o                where the .tgz is written (default: current directory)
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
umask 077

SINCE="24h"
TAIL="20000"
ALLOW_EXEC=0
OUT_DIR="$PWD"
LOCAL_PORT=18471

usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="$2"; shift ;;
    --tail) TAIL="$2"; shift ;;
    --exec) ALLOW_EXEC=1 ;;
    -f|--inputs) INPUTS_FILE="$2"; shift ;;
    -o|--out) OUT_DIR="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

need kubectl
need python3
need tar
[ "$ALLOW_EXEC" = 0 ] || need curl
load_inputs_env
[ -d "$OUT_DIR" ] || die "output directory does not exist: $OUT_DIR"

KUBECTL=(kubectl --context "$KUBE_CONTEXT" --request-timeout=30s)
IN_NS=("${KUBECTL[@]}" -n "$NAMESPACE")
LOGS_IN_NS=(kubectl --context "$KUBE_CONTEXT" --request-timeout=5m -n "$NAMESPACE")
"${KUBECTL[@]}" get --raw /version >/dev/null 2>&1 \
  || die "cluster unreachable on context '$KUBE_CONTEXT' — check your kubeconfig"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BUNDLE_NAME="nexus-support-$STAMP"
WORK_DIR="$(mktemp -d)"
BUNDLE="$WORK_DIR/$BUNDLE_NAME"
ERRORS="$BUNDLE/collection-errors.log"
FAILURES=0
FORWARD_PID=""
PREFLIGHT_VERDICT="not run"

cleanup() {
  [ -z "$FORWARD_PID" ] || kill "$FORWARD_PID" 2>/dev/null || true
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

mkdir -p "$BUNDLE"
: > "$ERRORS"

note() { printf -- '--- %s\n' "$*" >> "$ERRORS"; }

record_failure() {
  FAILURES=$((FAILURES + 1))
  printf -- '=== %s (exit %s)\n' "$1" "$2" >> "$ERRORS"
  [ -z "$3" ] || printf '%s\n' "$3" | sed 's/^/    /' >> "$ERRORS"
  return 0
}

# Runs a collector: stdout becomes the artifact, an empty artifact is dropped (a CRD
# the install never created, a container with no previous log). Absence is not a
# collection failure — a denial or a timeout is, or the bundle would report no
# failures while missing what it could not read.
ABSENT='doesn.t have a resource type|previous terminated container .* not found|\(NotFound\)|No resources found'

capture() {
  local dest="$1"; shift
  local stderr_text exit_code=0
  mkdir -p "$(dirname "$dest")"
  stderr_text="$("$@" 2>&1 >"$dest")" || exit_code=$?
  [ -s "$dest" ] || rm -f "$dest"
  if [ "$exit_code" -ne 0 ] && ! printf '%s\n' "$stderr_text" | grep -Eq "$ABSENT"; then
    record_failure "$*" "$exit_code" "$stderr_text"
  fi
  return 0
}

CLUSTER_RESOURCES=(storageclasses priorityclasses)
NAMESPACED_RESOURCES=(
  pods deployments statefulsets daemonsets replicasets jobs cronjobs
  services endpoints ingresses configmaps persistentvolumeclaims
  serviceaccounts roles rolebindings horizontalpodautoscalers poddisruptionbudgets
  externalsecrets foundationdbclusters
)

collect_cluster() {
  capture "$BUNDLE/cluster/version.yaml" "${KUBECTL[@]}" version -o yaml
  capture "$BUNDLE/cluster/nodes-wide.txt" "${KUBECTL[@]}" get nodes -o wide
  capture "$BUNDLE/cluster/nodes-describe.txt" "${KUBECTL[@]}" describe nodes
  capture "$BUNDLE/cluster/crds.txt" "${KUBECTL[@]}" get customresourcedefinitions
  for resource in "${CLUSTER_RESOURCES[@]}"; do
    capture "$BUNDLE/cluster/$resource.yaml" "${KUBECTL[@]}" get "$resource" -o yaml
  done
}

collect_namespace() {
  capture "$BUNDLE/namespace/overview.txt" "${IN_NS[@]}" get all -o wide
  capture "$BUNDLE/namespace/events.txt" "${IN_NS[@]}" get events --sort-by=.lastTimestamp
  capture "$BUNDLE/namespace/secrets-inventory.txt" "${IN_NS[@]}" get secrets
  capture "$BUNDLE/namespace/pods-describe.txt" "${IN_NS[@]}" describe pods
  for resource in "${NAMESPACED_RESOURCES[@]}"; do
    capture "$BUNDLE/namespace/objects/$resource.yaml" "${IN_NS[@]}" get "$resource" -o yaml
  done
}

pod_containers() {
  "${IN_NS[@]}" get pods -o jsonpath='{range .items[*]}{.metadata.name}{range .spec.initContainers[*]}{" "}{.name}{end}{range .spec.containers[*]}{" "}{.name}{end}{"\n"}{end}'
}

# Previous-container logs are capped by --tail alone: they usually predate the window,
# and --since can only narrow the result, never reach past --tail.
collect_logs() {
  local listing="$WORK_DIR/pod-containers.txt"
  capture "$listing" pod_containers
  [ -s "$listing" ] || return 0
  local pod containers container
  while read -r pod containers; do
    [ -n "$pod" ] || continue
    # shellcheck disable=SC2086  # container names are DNS labels
    for container in $containers; do
      capture "$BUNDLE/logs/$pod/$container.log" \
        "${LOGS_IN_NS[@]}" logs "$pod" -c "$container" --since "$SINCE" --tail "$TAIL"
      capture "$BUNDLE/logs/$pod/$container.previous.log" \
        "${LOGS_IN_NS[@]}" logs "$pod" -c "$container" --previous --tail "$TAIL"
    done
  done < "$listing"
}

collect_helm() {
  if ! command -v helm >/dev/null 2>&1; then
    note "helm not installed — release data not collected"
    return
  fi
  local helm_cmd=(helm --kube-context "$KUBE_CONTEXT" -n "$NAMESPACE")
  command -v timeout >/dev/null 2>&1 && helm_cmd=(timeout 60 "${helm_cmd[@]}")
  capture "$BUNDLE/helm/list.yaml" "${helm_cmd[@]}" list --all -o yaml
  capture "$BUNDLE/helm/status.txt" "${helm_cmd[@]}" status "$RELEASE"
  capture "$BUNDLE/helm/values.yaml" "${helm_cmd[@]}" get values "$RELEASE" --all
  capture "$BUNDLE/helm/manifest.yaml" "${helm_cmd[@]}" get manifest "$RELEASE"
}

# preflight.py exits non-zero when it finds problems, which is a verdict about the
# install and not a failure of this script.
collect_preflight() {
  local status=0
  python3 "$HERE/preflight.py" -f "$INPUTS_FILE" --gen-dir "$GEN_DIR" \
    > "$BUNDLE/inputs/preflight.txt" 2>&1 || status=$?
  if [ "$status" -eq 0 ]; then
    PREFLIGHT_VERDICT="pass"
  else
    PREFLIGHT_VERDICT="reported problems (exit $status) — see inputs/preflight.txt"
  fi
}

collect_inputs() {
  mkdir -p "$BUNDLE/inputs"
  if [ -f "$INPUTS_FILE" ]; then
    cp "$INPUTS_FILE" "$BUNDLE/inputs/customer.yaml" || note "could not copy $INPUTS_FILE"
  else
    note "inputs file not found: $INPUTS_FILE"
  fi
  cp -r "$GEN_DIR" "$BUNDLE/inputs/generated" || note "could not copy $GEN_DIR"
  collect_preflight
}

probe_gateway_version() {
  local service="$RELEASE-gateway" port
  port="$("${IN_NS[@]}" get "svc/$service" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null)" || true
  if [ -z "${port:-}" ]; then
    note "no gateway Service $service — version probe skipped"
    return
  fi
  "${IN_NS[@]}" port-forward "svc/$service" "$LOCAL_PORT:$port" >"$WORK_DIR/port-forward.log" 2>&1 &
  FORWARD_PID=$!
  capture "$BUNDLE/probes/version.txt" curl -sSi --max-time 20 \
    --retry 10 --retry-delay 1 --retry-connrefused "http://127.0.0.1:$LOCAL_PORT/api/v0/version"
  kill "$FORWARD_PID" 2>/dev/null || true
  wait "$FORWARD_PID" 2>/dev/null || true
  FORWARD_PID=""
  [ -f "$BUNDLE/probes/version.txt" ] \
    || record_failure "port-forward svc/$service" n/a "$(cat "$WORK_DIR/port-forward.log")"
}

foundationdb_pod() {
  "${IN_NS[@]}" get pods \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .spec.containers[*]}{.name}{","}{end}{"\n"}{end}' \
    2>/dev/null | awk -F'\t' '$2 ~ /(^|,)fdb(,|$)/ { print $1 }' | sed -n 1p
}

collect_foundationdb_status() {
  local pod
  pod="$(foundationdb_pod)"
  if [ -z "$pod" ]; then
    note "no FoundationDB pod found — fdbcli status skipped"
    return
  fi
  capture "$BUNDLE/fdb/status.json" \
    "${IN_NS[@]}" exec "$pod" -c fdb -- fdbcli -C /shared/fdb.cluster --exec 'status json'
}

write_summary() {
  local pods_running pods_total
  pods_total="$(wc -l < "$WORK_DIR/pod-containers.txt" 2>/dev/null || echo 0)"
  pods_running="$( { "${IN_NS[@]}" get pods --field-selector=status.phase=Running --no-headers 2>/dev/null || true; } | wc -l)"
  cat > "$BUNDLE/SUMMARY.txt" <<EOF
Nexus self-hosted support bundle
  collected (UTC)   : $STAMP
  context           : $KUBE_CONTEXT
  namespace         : $NAMESPACE
  release           : $RELEASE
  bundle tag        : $BUNDLE_TAG (DB/FDB image tags come from the chart render; see manifest.txt)
  log window        : --since $SINCE --tail $TAIL
  exec collectors   : $([ "$ALLOW_EXEC" = 1 ] && echo "enabled (version probe, fdbcli status)" || echo "disabled (pass --exec)")
  pods              : $pods_running running of $pods_total
  preflight         : $PREFLIGHT_VERDICT
  failed collectors : $FAILURES (see collection-errors.log)
  local tooling     : kubectl $(kubectl version --client -o yaml 2>/dev/null | sed -nE '/^  gitVersion: (.*)/{s//\1/p;q;}'), helm $(helm version --short 2>/dev/null || echo absent)

Secret values are not collected and every file was passed through redact.py; see
REDACTIONS.txt. Review the archive before sending it.
EOF
}

log "collecting into $BUNDLE_NAME (context $KUBE_CONTEXT, namespace $NAMESPACE)"
collect_cluster
collect_namespace
collect_logs
collect_helm
collect_inputs
if [ "$ALLOW_EXEC" = 1 ]; then
  probe_gateway_version
  collect_foundationdb_status
else
  note "read-only run — version probe and fdbcli status skipped"
fi

write_summary

log "redacting"
python3 "$HERE/redact.py" "$BUNDLE" > "$BUNDLE/REDACTIONS.txt" \
  || die "redaction failed — no archive written, so nothing unredacted can be sent"

ARCHIVE="$OUT_DIR/$BUNDLE_NAME.tgz"
tar czf "$ARCHIVE" -C "$WORK_DIR" "$BUNDLE_NAME"

[ "$FAILURES" -eq 0 ] || warn "$FAILURES collector(s) failed — see collection-errors.log in the bundle"
log "wrote $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
log "review it, then send it to Pinecone."
