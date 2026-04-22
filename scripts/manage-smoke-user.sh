#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-}"
shift || true

KEYCLOAK_URL="https://auth.bytepace.com"
KEYCLOAK_REALM="ssa"
KEYCLOAK_ADMIN_USER="admin"
KEYCLOAK_ADMIN_PASSWORD=""
USER_EMAIL=""
USER_PASSWORD=""
FIRST_NAME="Smoke"
LAST_NAME="Test"
DEFAULT_SMOKE_PASSWORD="SmokePass123!"
INSECURE=false

log() { echo "[smoke-user] $*"; }
fail() { echo "[smoke-user] ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  bash scripts/manage-smoke-user.sh create --keycloak-admin-password <password> [options]
  bash scripts/manage-smoke-user.sh delete --keycloak-admin-password <password> --email <email> [options]

Options:
  --keycloak-url <url>               Default: https://auth.bytepace.com
  --realm <realm>                    Default: ssa
  --keycloak-admin-user <user>       Default: admin
  --keycloak-admin-password <pass>   Required
  --email <email>                    Optional for create, required for delete
  --password <password>              Optional for create; generated if omitted
  --first-name <name>                Default: Smoke
  --last-name <name>                 Default: Test
  --insecure                         Pass -k to curl for local TLS troubleshooting
EOF
}

[[ -n "$ACTION" ]] || {
  usage
  exit 1
}

case "$ACTION" in
  create|delete) ;;
  *)
    usage
    fail "Unknown action: $ACTION"
    ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keycloak-url) KEYCLOAK_URL="$2"; shift 2 ;;
    --realm) KEYCLOAK_REALM="$2"; shift 2 ;;
    --keycloak-admin-user) KEYCLOAK_ADMIN_USER="$2"; shift 2 ;;
    --keycloak-admin-password) KEYCLOAK_ADMIN_PASSWORD="$2"; shift 2 ;;
    --email) USER_EMAIL="$2"; shift 2 ;;
    --password) USER_PASSWORD="$2"; shift 2 ;;
    --first-name) FIRST_NAME="$2"; shift 2 ;;
    --last-name) LAST_NAME="$2"; shift 2 ;;
    --insecure) INSECURE=true; shift ;;
    *)
      usage
      fail "Unknown option: $1"
      ;;
  esac
done

[[ -n "$KEYCLOAK_ADMIN_PASSWORD" ]] || fail "--keycloak-admin-password is required"

if [[ "$ACTION" == "create" ]]; then
  [[ -n "$USER_EMAIL" ]] || USER_EMAIL="smoke-$(date +%s)@bytepace.test"
  [[ -n "$USER_PASSWORD" ]] || USER_PASSWORD="$DEFAULT_SMOKE_PASSWORD"
else
  [[ -n "$USER_EMAIL" ]] || fail "--email is required for delete"
fi

CURL_ARGS=(-fsS)
if [[ "$INSECURE" == "true" ]]; then
  CURL_ARGS+=(-k)
fi

TOKEN_RESPONSE=$(curl "${CURL_ARGS[@]}" -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "client_id=admin-cli" \
  --data-urlencode "grant_type=password" \
  --data-urlencode "username=${KEYCLOAK_ADMIN_USER}" \
  --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}") || fail "Failed to obtain admin token${INSECURE:+ even with --insecure}"

TOKEN=$(printf '%s' "$TOKEN_RESPONSE" | jq -r '.access_token // empty')
[[ -n "$TOKEN" ]] || fail "Admin token is empty"

kc_get() {
  curl "${CURL_ARGS[@]}" -H "Authorization: Bearer ${TOKEN}" "$@"
}

kc_post() {
  curl "${CURL_ARGS[@]}" -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" -X POST "$@"
}

kc_put() {
  curl "${CURL_ARGS[@]}" -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" -X PUT "$@"
}

kc_delete() {
  curl "${CURL_ARGS[@]}" -H "Authorization: Bearer ${TOKEN}" -X DELETE "$@"
}

USER_LOOKUP=$(kc_get "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users?username=${USER_EMAIL}")
USER_ID=$(printf '%s' "$USER_LOOKUP" | jq -r '.[0].id // empty')

if [[ "$ACTION" == "delete" ]]; then
  if [[ -z "$USER_ID" ]]; then
    log "User not found, nothing to delete: ${USER_EMAIL}"
    exit 0
  fi
  kc_delete "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users/${USER_ID}" >/dev/null
  log "Deleted smoke user: ${USER_EMAIL}"
  exit 0
fi

if [[ -z "$USER_ID" ]]; then
  kc_post "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users" -d @- >/dev/null <<EOF
{
  "username": "${USER_EMAIL}",
  "email": "${USER_EMAIL}",
  "emailVerified": true,
  "enabled": true,
  "firstName": "${FIRST_NAME}",
  "lastName": "${LAST_NAME}",
  "requiredActions": []
}
EOF
  USER_LOOKUP=$(kc_get "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users?username=${USER_EMAIL}")
  USER_ID=$(printf '%s' "$USER_LOOKUP" | jq -r '.[0].id // empty')
fi

[[ -n "$USER_ID" ]] || fail "Could not resolve smoke user id after create/update"

kc_get "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users/${USER_ID}" \
  | jq --arg first "$FIRST_NAME" --arg last "$LAST_NAME" --arg email "$USER_EMAIL" '
      .enabled = true
      | .email = $email
      | .emailVerified = true
      | .firstName = $first
      | .lastName = $last
      | .requiredActions = []
    ' \
  | kc_put "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users/${USER_ID}" -d @- >/dev/null

kc_put "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users/${USER_ID}/reset-password" -d @- >/dev/null <<EOF
{
  "type": "password",
  "value": "${USER_PASSWORD}",
  "temporary": false
}
EOF

log "Smoke user ready"
log "email=${USER_EMAIL}"
log "password=${USER_PASSWORD}"
