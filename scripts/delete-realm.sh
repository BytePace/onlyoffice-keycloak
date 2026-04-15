#!/usr/bin/env bash
# delete-realm.sh — Remove OnlyOffice OIDC clients from the shared Keycloak realm (default ssa).
# Does NOT delete the realm (Grist and other apps keep their clients).

set -uo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:?Error: KEYCLOAK_URL required}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:?Error: KEYCLOAK_ADMIN_PASSWORD required}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-ssa}"
REALM="$KEYCLOAK_REALM"

log()     { echo "[delete-oo-clients] $*"; }
success() { echo "[delete-oo-clients] ✓ $*"; }
warn()    { echo "[delete-oo-clients] WARN: $*" >&2; }
fail()    { echo "[delete-oo-clients] ERROR: $*" >&2; exit 1; }

if [[ ! "$KEYCLOAK_URL" =~ ^https?:// ]]; then
    KEYCLOAK_URL="https://${KEYCLOAK_URL}"
    log "Added https:// to Keycloak URL: ${KEYCLOAK_URL}"
fi

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

delete_client_by_client_id() {
    local client_id="$1"
    local uuid http_code

    uuid=$(curl -s -H "Authorization: Bearer ${TOKEN}" \
        "${KEYCLOAK_URL}/admin/realms/${REALM}/clients?clientId=${client_id}" \
        | jq -r '.[0].id // empty')

    if [[ -z "$uuid" || "$uuid" == "null" ]]; then
        warn "Client '${client_id}' not found in realm '${REALM}' — skipping."
        return 0
    fi

    http_code=$(curl -sS -o /tmp/oo-del-client.txt -w "%{http_code}" -X DELETE \
        "${KEYCLOAK_URL}/admin/realms/${REALM}/clients/${uuid}" \
        -H "Authorization: Bearer ${TOKEN}")
    if [[ "$http_code" == "204" ]] || [[ "$http_code" == "200" ]]; then
        success "Deleted client '${client_id}' (realm '${REALM}' preserved)"
    else
        warn "DELETE '${client_id}' returned HTTP ${http_code}: $(cat /tmp/oo-del-client.txt 2>/dev/null || true)"
    fi
}

log "Removing OnlyOffice clients from realm '${REALM}' (not deleting the realm)..."
delete_client_by_client_id "onlyoffice-mobile"
delete_client_by_client_id "onlyoffice-client"
success "OnlyOffice OIDC clients cleanup finished for realm '${REALM}'."
