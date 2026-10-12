#!/usr/bin/env bash
# End-to-end test of the server as an OAuth resource server, against a real identity
# provider (authentik), decision record 0002:
#
#   no token                → 401, and the challenge says where the metadata is
#   the metadata            → names this server and its authorization server
#   a user's token          → the tool answers, and knows who is calling
#   a token without scope   → 403 insufficient_scope
#   another app's token     → 401 (valid, signed by the same provider, not for us)
#
#   scripts/auth_e2e.sh          # builds, runs the checks, tears everything down
#   KEEP=1 scripts/auth_e2e.sh   # leave the stack up afterwards
#
# Needs Docker and ports 8080 and 9000 free. CI runs it on every pull request.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
project="mcpidp$$"
work="$(mktemp -d)"
env_file="$work/env"
rand() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
admin_token="$(rand)"
cat > "$env_file" <<ENV
AUTHENTIK_SECRET_KEY=$(rand)
AUTHENTIK_BOOTSTRAP_PASSWORD=$(rand)
AUTHENTIK_BOOTSTRAP_TOKEN=$admin_token
ENV

dc() { docker compose -p "$project" --env-file "$env_file" -f docker-compose.yml -f docker-compose.idp.yml "$@"; }
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "--- auth e2e FAILED; recent logs:" >&2
    dc ps -a >&2 || true
    dc logs --tail 40 mcp >&2 || true
    # authentik logs every request and long stack frames: keep what says what went wrong.
    dc logs --no-color authentik-worker authentik-server 2>&1 \
      | grep -E '"level": ?"(warning|error|critical)"|Traceback|mcp-atlassian' \
      | grep -vE '/health/|"logger": ?"authentik.asgi"|Task (enqueued|started)' | cut -c1-900 | tail -40 >&2 || true
  fi
  if [ -z "${KEEP:-}" ]; then dc down -v --remove-orphans > /dev/null 2>&1 || true; fi
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT
ok() { echo "  ok   $*"; }
note() { echo "  note $*"; }
fail() { echo "  FAIL $*" >&2; exit 1; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

mcp="http://localhost:8080"
idp="http://localhost:9000"   # localhost, not 127.0.0.1: it becomes the tokens' issuer
app="$idp/application/o/mcp-atlassian"
api() {  # api METHOD PATH [JSON] → body; fails on an HTTP error
  local method="$1" path="$2" body="${3:-}"
  curl -sS --fail-with-body --max-time 30 -X "$method" "$idp/api/v3$path" \
    -H "Authorization: Bearer $admin_token" -H 'content-type: application/json' \
    ${body:+-d "$body"}
}
wait_for() {  # wait_for SECONDS DESCRIPTION COMMAND…
  local seconds="$1" what="$2"; shift 2
  for _ in $(seq 1 "$seconds"); do "$@" > /dev/null 2>&1 && return 0; sleep 1; done
  fail "timed out after ${seconds}s waiting for: $what"
}

echo "start (authentik takes a minute or two to migrate on first boot)"
dc up -d --build > "$work/up.log" 2>&1 || { tail -30 "$work/up.log" >&2; fail "compose up"; }
wait_for 120 "the MCP server" curl -sf "$mcp/healthz"
ok "the server is up"
wait_for 300 "authentik" curl -sf "$idp/-/health/ready/"
wait_for 240 "the admin API token" api GET "/core/users/?username=akadmin"
state=""
for _ in $(seq 1 180); do
  state="$(api GET "/managed/blueprints/?search=mcp-atlassian.yaml" | json '(d["results"] or [{}])[0].get("status", "")' 2>/dev/null || true)"
  case "$state" in
    successful) break ;;
    error) api GET "/managed/blueprints/?search=mcp-atlassian.yaml" >&2; fail "authentik rejected the blueprint" ;;
  esac
  sleep 2
done
[ "$state" = successful ] || fail "the blueprint was not applied (status: '${state:-not discovered}')"
wait_for 60 "the OAuth application" curl -sf "$app/.well-known/openid-configuration"
ok "authentik is up, the blueprint is applied"

# --- a client with no token: discovery --------------------------------------------------
initialize='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"e2e","version":"0"}}}'
post() {  # post TOKEN BODY [SESSION] → status; headers in $work/h, body in $work/b
  local token="$1" body="$2" session="${3:-}"
  curl -s --max-time 30 -o "$work/b" -D "$work/h" -w '%{http_code}' "$mcp/mcp" \
    -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
    ${token:+-H "Authorization: Bearer $token"} ${session:+-H "mcp-session-id: $session"} -d "$body"
}
header() { grep -i "^$1:" "$work/h" | head -1 | cut -d' ' -f2- | tr -d '\r'; }

[ "$(post "" "$initialize")" = 401 ] || fail "no token should be a 401"
challenge="$(header www-authenticate)"
meta_url="$(echo "$challenge" | sed -n 's/.*resource_metadata="\([^"]*\)".*/\1/p')"
[ -n "$meta_url" ] || fail "the 401 has no resource_metadata: $challenge"
ok "no token: 401, and the challenge points at $meta_url"

meta="$(curl -sf "$meta_url")" || fail "the resource metadata is not served"
[ "$(echo "$meta" | json 'd["resource"]')" = "$mcp/mcp" ] || fail "resource: $meta"
[ "$(echo "$meta" | json 'd["authorization_servers"][0]')" = "$app/" ] || fail "authorization_servers: $meta"
ok "the metadata names this server and its authorization server"

as_meta="$(curl -sf "$app/.well-known/openid-configuration")" || fail "the authorization server has no discovery document"
[ "$(echo "$as_meta" | json 'd["issuer"]')" = "$app/" ] || fail "issuer mismatch"
echo "$as_meta" | json '"S256" in d.get("code_challenge_methods_supported", [])' | grep -q True || fail "the authorization server doesn't advertise PKCE (S256)"
ok "the authorization server is discoverable and supports PKCE (S256)"
note "it advertises: registration endpoint=$(echo "$as_meta" | json '"registration_endpoint" in d'), client id metadata documents=$(echo "$as_meta" | json 'd.get("client_id_metadata_document_supported", False)'), iss in responses=$(echo "$as_meta" | json 'd.get("authorization_response_iss_parameter_supported", False)')"

# --- a user, and tokens the way a script gets them --------------------------------------
user="$(api POST /core/users/ '{"username":"alice","name":"Alice Example","email":"alice@example.com","is_active":true}' | json 'd["pk"]')"
api POST /core/tokens/ "{\"identifier\":\"alice-e2e\",\"intent\":\"app_password\",\"user\":$user,\"expiring\":false}" > /dev/null
password="$(api GET /core/tokens/alice-e2e/view_key/ | json 'd["key"]')"
token() {  # token CLIENT_ID SCOPE [EXTRA curl args] → an access token for alice
  local client="$1" scope="$2"; shift 2
  curl -sS --fail-with-body --max-time 30 "$idp/application/o/token/" \
    -d grant_type=client_credentials -d "client_id=$client" \
    -d username=alice --data-urlencode "password=$password" --data-urlencode "scope=$scope" "$@" \
    | json 'd["access_token"]'
}
claims() { python3 -c "
import base64, json, sys
body = sys.argv[1].split('.')[1]
print(json.dumps(json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))))" "$1"; }

access="$(token mcp-atlassian 'openid profile atlassian:read')" || fail "no access token for alice"
c="$(claims "$access")"
[ "$(echo "$c" | json 'd["aud"]')" = mcp-atlassian ] || fail "aud: $c"
echo "$c" | json '"atlassian:read" in d.get("scope", "").split()' | grep -q True || fail "the token has no atlassian:read scope: $c"
ok "alice has a token for audience mcp-atlassian with scope atlassian:read"

# --- the tool, as alice -----------------------------------------------------------------
[ "$(post "$access" "$initialize")" = 200 ] || { cat "$work/b" >&2; fail "initialize with a valid token"; }
session="$(header mcp-session-id)"
post "$access" '{"jsonrpc":"2.0","method":"notifications/initialized"}' "$session" > /dev/null
[ "$(post "$access" '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"server_status","arguments":{}}}' "$session")" = 200 ] || fail "tools/call"
status="$(grep '^data: ' "$work/b" | head -1 | cut -c7- | json 'json.dumps(d["result"]["structuredContent"])')"
[ "$(echo "$status" | json 'd["signed_in_as"]')" = "$(echo "$c" | json 'd["sub"]')" ] || fail "signed_in_as: $status"
ok "the tool answers and knows who is calling ($(echo "$status" | json 'd["signed_in_as"][:12]')…)"

# --- tokens that must not work ----------------------------------------------------------
unscoped="$(token mcp-atlassian 'openid profile')" || fail "no unscoped token"
[ "$(post "$unscoped" "$initialize")" = 403 ] || fail "a token without the scope should be a 403"
header www-authenticate | grep -q 'error="insufficient_scope"' || fail "the 403 doesn't say insufficient_scope"
ok "a token without atlassian:read: 403 insufficient_scope"

other="$(token some-other-app 'openid atlassian:read')" || fail "no token for the other app"
[ "$(claims "$other" | json 'd["aud"]')" = some-other-app ] || fail "the other app's token has the wrong audience"
[ "$(post "$other" "$initialize")" = 401 ] || fail "another app's token should be a 401"
ok "a valid token for another application, with the same scope: 401"

[ "$(post "$access.tampered" "$initialize")" = 401 ] || fail "a tampered token should be a 401"
ok "a tampered token: 401"

# --- what this identity provider does with RFC 8707 (informational) ---------------------
bound="$(token mcp-atlassian 'openid atlassian:read' --data-urlencode "resource=$mcp/mcp" 2>/dev/null || true)"
if [ -n "$bound" ]; then
  note "token requested with resource=$mcp/mcp has aud=$(claims "$bound" | json 'd["aud"]')"
else
  note "the token endpoint refused a request with a resource parameter"
fi

echo "auth e2e passed"
