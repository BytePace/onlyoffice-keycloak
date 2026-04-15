#!/usr/bin/env bash
# delete-realm.sh — Delete the 'onlyoffice' realm from Keycloak

set -uo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:?Error: KEYCLOAK_URL required}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:?Error: KEYCLOAK_ADMIN_PASSWORD required}"
REALM="onlyoffice"

log()  { echo "[delete-realm] $*"; }
warn() { echo "[delete-realm] WARN: $*" >&2; }
fail() { echo "[delete-realm] ERROR: $*" >&2; exit 1; }

# ── Ensure KEYCLOAK_URL has https:// ──────────────────────────────────────────
if [[ ! "$KEYCLOAK_URL" =~ ^https?:// ]]; then
    KEYCLOAK_URL="https://${KEYCLOAK_URL}"
    log "Added https:// to Keycloak URL: ${KEYCLOAK_URL}"
fi

# ── Get admin token ───────────────────────────────────────────────────────────
log "Obtaining admin token from ${KEYCLOAK_URL}..."
TOKEN_RESPONSE=$(curl -sfL -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "client_id=admin-cli&grant_type=password&username=admin&password=${KEYCLOAK_ADMIN_PASSWORD}" 2>/tmp/keycloak-curl.err || true)

if [[ -z "$TOKEN_RESPONSE" ]]; then
    warn "Token response is empty. Error: $(cat /tmp/keycloak-curl.err 2>/dev/null || echo 'unknown')"
    fail "Failed to obtain admin token"
fi

TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.access_token // empty')
[[ -z "$TOKEN" || "$TOKEN" == "null" ]] && fail "Failed to obtain admin token. Response: $TOKEN_RESPONSE"
log "Admin token obtained."

# ── Delete realm ──────────────────────────────────────────────────────────────
log "Deleting realm '${REALM}' from Keycloak..."

DELETE_RESPONSE=$(curl -sf -X DELETE "${KEYCLOAK_URL}/admin/realms/${REALM}" \
    -H "Authorization: Bearer ${TOKEN}" 2>&1 || true)

if [[ $? -eq 0 ]]; then
    log "Realm '${REALM}' deleted successfully"
else
    warn "Could not delete realm (may not exist). Response: $DELETE_RESPONSE"
fi

log "Done."
