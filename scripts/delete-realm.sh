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

TOKEN_RESPONSE=$(curl -s -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "client_id=admin-cli&grant_type=password&username=admin&password=${KEYCLOAK_ADMIN_PASSWORD}")

TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.access_token // empty' 2>/dev/null || echo "")

if [[ -z "$TOKEN" ]]; then
    error=$(echo "$TOKEN_RESPONSE" | jq -r '.error_description // .error // "unknown"' 2>/dev/null || echo "unknown")
    fail "Failed to obtain admin token: $error"
fi

log "Admin token obtained."

# ── Delete realm ──────────────────────────────────────────────────────────────
log "Deleting realm '${REALM}' from Keycloak..."

DELETE_RESPONSE=$(curl -s -w "\n%{http_code}" -X DELETE "${KEYCLOAK_URL}/admin/realms/${REALM}" \
    -H "Authorization: Bearer ${TOKEN}")

HTTP_CODE=$(echo "$DELETE_RESPONSE" | tail -1)

if [[ "$HTTP_CODE" == "204" ]] || [[ "$HTTP_CODE" == "200" ]]; then
    log "Realm '${REALM}' deleted successfully"
else
    warn "Could not delete realm (HTTP $HTTP_CODE - may not exist)"
fi

log "Done."
