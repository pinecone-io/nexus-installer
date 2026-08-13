#!/usr/bin/env bash
# Post-install functional smoke test — proves the whole chain end to end:
# login -> create context -> import a document -> curate -> retrieval -> chat
# query. It asserts semantic retrieval actually returns hits and that a grounded,
# cited answer comes back. A passing run confirms the model endpoints (embedding
# on ingest, chat + rerank on query) are wired and the index is serving results.
#
# It talks to the gateway (the single front door the ingress will front), so it
# also exercises the nexus-auth auth_request the ingress relies on. By default it
# port-forwards svc/nexus-gateway itself; point BASE_URL at an already-reachable
# gateway (e.g. once the ingress is live) to skip the port-forward.
#
# The login credential is NEXUS_SESSION_CREDENTIAL, read from the environment or
# from install/.secrets.env (written by install.sh). It is never printed.
#
# Usage:
#   ./smoke-test.sh [--slug NAME] [--keep] [--base-url URL] [--port N]
#     --slug NAME     context slug to use (default: smoke-check)
#     --keep          leave the smoke context behind (default: delete it on pass)
#     --base-url URL  talk to this gateway URL instead of port-forwarding
#     --port N        local port for the port-forward (default: 8080)
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

SLUG="smoke-check"
KEEP=0
BASE_URL=""
LOCAL_PORT=8080

while [ $# -gt 0 ]; do
  case "$1" in
    --slug)     SLUG="$2"; shift ;;
    --keep)     KEEP=1 ;;
    --base-url) BASE_URL="$2"; shift ;;
    --port)     LOCAL_PORT="$2"; shift ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

need curl
need jq

# --- credential (never printed) ----------------------------------------------
if [ -z "${NEXUS_SESSION_CREDENTIAL:-}" ]; then
  SECRETS_ENV="$HERE/.secrets.env"
  [ -f "$SECRETS_ENV" ] || die "NEXUS_SESSION_CREDENTIAL is not set and $SECRETS_ENV is missing — run install.sh first or export it."
  # shellcheck disable=SC1090
  source "$SECRETS_ENV"
  [ -n "${NEXUS_SESSION_CREDENTIAL:-}" ] || die "$SECRETS_ENV does not define NEXUS_SESSION_CREDENTIAL"
fi

# --- reach the gateway -------------------------------------------------------
PF_PID=""
cleanup() { [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true; }
trap cleanup EXIT

if [ -n "$BASE_URL" ]; then
  BASE="${BASE_URL%/}"
  log "using gateway at $BASE"
else
  need kubectl
  load_inputs_env   # KUBE_CONTEXT, NAMESPACE from generated/inputs.env
  BASE="http://localhost:$LOCAL_PORT"
  log "port-forwarding svc/nexus-gateway $LOCAL_PORT:80 (ns=$NAMESPACE, context=$KUBE_CONTEXT)"
  kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" \
    port-forward svc/nexus-gateway "$LOCAL_PORT:80" >/dev/null 2>&1 &
  PF_PID=$!
fi

# /api/v0/version is auth-exempt, so it works as a pre-login readiness probe.
for _ in $(seq 1 30); do
  [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' "$BASE/api/v0/version")" = "200" ] && break
  sleep 1
done
[ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$BASE/api/v0/version")" = "200" ] \
  || die "gateway not reachable at $BASE (is the release installed and healthy?)"
log "gateway reachable at $BASE"

# --- helpers -----------------------------------------------------------------
api() {
  local method="$1" path="$2"; shift 2
  curl -s -m 60 -X "$method" "$BASE$path" -H "$AUTH" "$@"
}

# Poll until .$field matches $good (success, echoes the body) or $bad (failure).
poll() {
  local path="$1" field="$2" good="$3" bad="$4" to="$5" t=0 body st
  while [ "$t" -lt "$to" ]; do
    body="$(curl -s -m 15 "$BASE$path" -H "$AUTH")"
    st="$(printf '%s' "$body" | jq -r ".$field // \"?\"")"
    if printf '%s' "$st" | grep -qE "$good"; then printf '%s' "$body"; return 0; fi
    if printf '%s' "$st" | grep -qE "$bad"; then
      warn "terminal state '$st' for $path"; printf '%s' "$body" | jq -r '.error // empty' >&2
      return 1
    fi
    sleep 3; t=$((t + 3))
  done
  warn "timeout after ${to}s polling $path (last $field=$st)"; return 2
}

# --- 1. login ----------------------------------------------------------------
TOKEN="$(jq -n --arg k "$NEXUS_SESSION_CREDENTIAL" '{api_key:$k}' \
  | curl -s -m 20 -X POST "$BASE/api/auth/login" \
      -H 'Content-Type: application/json' --data-binary @- \
  | jq -r '.token // empty')"
[ -n "$TOKEN" ] || die "login failed — check NEXUS_SESSION_CREDENTIAL"
AUTH="Authorization: Bearer $TOKEN"
log "1/6 login OK"

# --- 2. create context (tolerant of a re-run) --------------------------------
CREATE="$(api POST /api/contexts -H 'Content-Type: application/json' \
  -d "{\"slug\":\"$SLUG\",\"name\":\"Smoke Check\"}")"
if [ "$(printf '%s' "$CREATE" | jq -r '.slug // empty')" = "$SLUG" ]; then
  log "2/6 created context '$SLUG'"
elif api GET "/api/contexts/$SLUG" | jq -e '.slug' >/dev/null 2>&1; then
  log "2/6 context '$SLUG' already exists — reusing"
else
  die "could not create or find context '$SLUG': $(printf '%s' "$CREATE" | jq -r '.error // .')"
fi

# --- 3. import a document ----------------------------------------------------
DOC="$(mktemp)"; trap 'rm -f "$DOC"; cleanup' EXIT
printf 'The Larkspur Line ticket price is 4 guilders.\n' > "$DOC"
IMPORT_TASK="$(api POST "/api/contexts/$SLUG/import/upload" -F "file=@$DOC;filename=doc1.txt" \
  | jq -r '.task_id // empty')"
[ -n "$IMPORT_TASK" ] || die "import upload did not return a task_id"
BODY="$(poll "/api/tasks/$IMPORT_TASK" state '^completed$' '^(failed|cancelled)$' 120)" \
  || die "import task did not complete"
printf '%s' "$BODY" | jq -e '.output.status == "success" and .output.failed == 0 and .output.imported_items >= 1' \
  >/dev/null || die "import completed but did not import the document cleanly"
log "3/6 import OK (imported_items=$(printf '%s' "$BODY" | jq -r '.output.imported_items'))"

# --- 4. curate ---------------------------------------------------------------
CURATE_TASK="$(api POST "/api/contexts/$SLUG/curate" -H 'Content-Type: application/json' -d '{}' \
  | jq -r '.task_id // empty')"
[ -n "$CURATE_TASK" ] || die "curate did not return a task_id"
BODY="$(poll "/api/tasks/$CURATE_TASK" state '^completed$' '^(failed|cancelled)$' 300)" \
  || die "curate task did not complete"
printf '%s' "$BODY" | jq -e '.output.flipped == true and .output.delta.failed == 0 and .output.delta.chunks_created >= 1' \
  >/dev/null || die "curate completed but produced no searchable chunks"
log "4/6 curate OK (chunks_created=$(printf '%s' "$BODY" | jq -r '.output.delta.chunks_created'))"

# --- 5. retrieval-only query -------------------------------------------------
# Chat can ground on a fetched source file, so a cited answer alone can mask a
# dead index; assert retrieval itself returns hits.
RQID="$(api POST /api/query -H 'Content-Type: application/json' \
  -d "{\"ask\":\"What is the Larkspur Line ticket price?\",\"scope\":[\"$SLUG\"],\"retrieval_only\":true,\"background\":true}" \
  | jq -r '.id // empty')"
[ -n "$RQID" ] || die "retrieval query was not accepted"
BODY="$(poll "/api/queries/$RQID" status '^completed$' '^(failed|error|cancelled)$' 120)" \
  || die "retrieval query did not complete"
HITS="$(printf '%s' "$BODY" | jq -r '.rollup.total_hits // 0')"
[ "$HITS" -ge 1 ] \
  || die "retrieval returned 0 hits — the semantic index is not serving results (db-slim slab-serving / pc-lease); chat can still answer via file fetch, so this is a real failure."
log "5/6 retrieval OK (total_hits=$HITS)"

# --- 6. chat query -----------------------------------------------------------
# A cited answer is the check that proves the model endpoints are wired:
# embedding on ingest, and chat + rerank through the inference proxy on query.
QID="$(api POST /api/query -H 'Content-Type: application/json' \
  -d "{\"ask\":\"What is the Larkspur Line ticket price?\",\"scope\":[\"$SLUG\"],\"model\":\"standard\",\"background\":true}" \
  | jq -r '.id // empty')"
[ -n "$QID" ] || die "query was not accepted"
BODY="$(poll "/api/queries/$QID" status '^completed$' '^(failed|error|cancelled)$' 120)" \
  || die "query did not complete"
ANSWER="$(printf '%s' "$BODY" | jq -r '[.output[]?.content[]?.text] | join("")')"
CITES="$(printf '%s' "$BODY" | jq -r '.citations | length')"
[ -n "$ANSWER" ] && [ "$CITES" -ge 1 ] \
  || die "query returned no grounded answer (answer empty or no citations) — check the model endpoints"
log "6/6 chat OK — answer: $ANSWER"

# --- cleanup -----------------------------------------------------------------
if [ "$KEEP" = 1 ]; then
  log "leaving context '$SLUG' in place (--keep)"
else
  api DELETE "/api/contexts/$SLUG" >/dev/null 2>&1 || warn "could not delete context '$SLUG'"
  log "removed context '$SLUG'"
fi

printf '\033[32m[install] SMOKE TEST PASSED\033[0m\n' >&2
