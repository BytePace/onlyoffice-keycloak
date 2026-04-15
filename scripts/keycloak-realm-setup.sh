#!/usr/bin/env bash
# keycloak-realm-setup.sh — Creates realm "onlyoffice" with two OIDC clients
# in an existing or newly-deployed Keycloak instance.
#
# Required env vars (or args):
#   KEYCLOAK_URL            — e.g. http://127.0.0.1:8090 (internal) or https://auth.example.com
#   KEYCLOAK_ADMIN_PASSWORD — admin password
#   APP_DOMAIN              — public domain of the application (for redirect URIs)
#   MOBILE_REDIRECT_URI     — iOS custom-scheme callback URI
#
# Outputs:
#   /tmp/oo-client-secret.txt — secret for onlyoffice-client

set -uo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:?}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:?}"
APP_DOMAIN="${APP_DOMAIN:?}"
MOBILE_REDIRECT_URI="${MOBILE_REDIRECT_URI:-com.bytepace.scan-it-to-google-sheets://oauth/callback}"
EMAIL_USER="${EMAIL_USER:-}"
EMAIL_PASSWORD="${EMAIL_PASSWORD:-}"
EMAIL_HOST="${EMAIL_HOST:-smtp.gmail.com}"
EMAIL_PORT="${EMAIL_PORT:-587}"
REALM="onlyoffice"
MAX_WAIT=120

log()  { echo "[keycloak-setup] $*" | tee -a /tmp/keycloak-setup.log; }
warn() { echo "[keycloak-setup] WARN: $*" >&2 | tee -a /tmp/keycloak-setup.log; }
fail() { echo "[keycloak-setup] ERROR: $*" >&2 | tee -a /tmp/keycloak-setup.log; exit 1; }

# ── Ensure KEYCLOAK_URL has https:// ──────────────────────────────────────────
if [[ ! "$KEYCLOAK_URL" =~ ^https?:// ]]; then
    KEYCLOAK_URL="https://${KEYCLOAK_URL}"
    log "Added https:// to Keycloak URL: ${KEYCLOAK_URL}"
fi

# ── Wait for Keycloak ─────────────────────────────────────────────────────────
log "Waiting for Keycloak at ${KEYCLOAK_URL} ..."
elapsed=0
until curl -sfL "${KEYCLOAK_URL}/realms/master/.well-known/openid-configuration" >/dev/null 2>&1; do
    sleep 3; elapsed=$((elapsed + 3))
    [[ $elapsed -ge $MAX_WAIT ]] && fail "Keycloak did not become ready in ${MAX_WAIT}s"
done
log "Keycloak is ready."

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

auth_header() { echo "Authorization: Bearer ${TOKEN}"; }

kc_get()  { curl -sf  -H "$(auth_header)" "$@"; }
kc_post() { curl -sf  -H "$(auth_header)" -H "Content-Type: application/json" -X POST  "$@"; }
kc_put()  { curl -sf  -H "$(auth_header)" -H "Content-Type: application/json" -X PUT   "$@"; }

# ── Update realm with SMTP configuration ──────────────────────────────────────
update_realm_smtp() {
    local token="$1"

    if [[ -z "$EMAIL_USER" ]]; then
        log "SMTP not configured (EMAIL_USER is empty) — skipping."
        return 0
    fi

    log "Configuring SMTP for realm '${REALM}'..."

    local realm_json merged http_code put_body
    realm_json=$(curl -s -H "Authorization: Bearer $token" "${KEYCLOAK_URL}/admin/realms/${REALM}")
    if [[ -z "$realm_json" ]] || ! echo "$realm_json" | jq -e . >/dev/null 2>&1; then
        warn "Could not read realm for SMTP configuration (skipping)"
        return 0
    fi

    local port_num="${EMAIL_PORT:-587}"
    [[ "$port_num" =~ ^[0-9]+$ ]] || port_num=587

    merged=$(echo "$realm_json" | jq \
        --arg h "$EMAIL_HOST" \
        --argjson p "$port_num" \
        --arg u "$EMAIL_USER" \
        --arg pw "$EMAIL_PASSWORD" \
        '.smtpServer = {
            host: $h,
            port: $p,
            auth: true,
            starttls: true,
            user: $u,
            password: $pw,
            from: $u
        }
        | .verifyEmail = false
        | .resetPasswordAllowed = true') || {
        warn "Failed to assemble SMTP JSON (jq). Configure SMTP manually in Keycloak admin console."
        return 0
    }

    http_code=$(curl -sS -o /tmp/smtp-response.txt -w "%{http_code}" -X PUT \
        "${KEYCLOAK_URL}/admin/realms/${REALM}" \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json; charset=UTF-8" \
        -d "$merged")
    put_body=$(cat /tmp/smtp-response.txt 2>/dev/null || echo "")

    if [[ "$http_code" == "204" ]] || [[ "$http_code" == "200" ]]; then
        log "SMTP configured for realm '${REALM}' (host: ${EMAIL_HOST}:${EMAIL_PORT})"
    else
        warn "SMTP configuration failed (HTTP $http_code). Configure manually: Realm settings → Email. Response: $put_body"
    fi
}

# ── Create realm ──────────────────────────────────────────────────────────────
existing_realm=$(kc_get "${KEYCLOAK_URL}/admin/realms/${REALM}" 2>/dev/null | jq -r '.realm // empty')
if [[ "$existing_realm" == "$REALM" ]]; then
    log "Realm '${REALM}' already exists — skipping creation."
else
    log "Creating realm '${REALM}' ..."
    kc_post "${KEYCLOAK_URL}/admin/realms" -d @- <<EOF
{
  "realm": "${REALM}",
  "enabled": true,
  "registrationAllowed": true,
  "resetPasswordAllowed": true,
  "verifyEmail": false,
  "ssoSessionIdleTimeout": 3600,
  "ssoSessionMaxLifespan": 36000,
  "offlineSessionIdleTimeout": 604800
}
EOF
    log "Realm '${REALM}' created."
fi

# ── Configure SMTP for realm ──────────────────────────────────────────────────
update_realm_smtp "$TOKEN"

# ── Helper: create or skip client ────────────────────────────────────────────
create_client_if_missing() {
    local client_id="$1"
    local payload="$2"
    local existing
    existing=$(kc_get "${KEYCLOAK_URL}/admin/realms/${REALM}/clients?clientId=${client_id}" \
        | jq -r '.[0].id // empty')
    if [[ -n "$existing" ]]; then
        log "Client '${client_id}' already exists — skipping."
    else
        log "Creating client '${client_id}' ..."
        kc_post "${KEYCLOAK_URL}/admin/realms/${REALM}/clients" -d "$payload"
        log "Client '${client_id}' created."
    fi
}

# ── onlyoffice-client (confidential — used by the API to validate tokens) ────
create_client_if_missing "onlyoffice-client" '{
  "clientId": "onlyoffice-client",
  "name": "OnlyOffice Spreadsheet API",
  "enabled": true,
  "protocol": "openid-connect",
  "publicClient": false,
  "standardFlowEnabled": true,
  "directAccessGrantsEnabled": true,
  "serviceAccountsEnabled": true,
  "redirectUris": ["https://'"${APP_DOMAIN}"'/api/*"],
  "webOrigins": ["https://'"${APP_DOMAIN}"'"],
  "attributes": {"post.logout.redirect.uris": "https://'"${APP_DOMAIN}"'/api/signed-out"}
}'

# Retrieve the generated client secret
CLIENT_INTERNAL_ID=$(kc_get "${KEYCLOAK_URL}/admin/realms/${REALM}/clients?clientId=onlyoffice-client" \
    | jq -r '.[0].id')
[[ -z "$CLIENT_INTERNAL_ID" || "$CLIENT_INTERNAL_ID" == "null" ]] && fail "Could not find onlyoffice-client"

SECRET=$(kc_get "${KEYCLOAK_URL}/admin/realms/${REALM}/clients/${CLIENT_INTERNAL_ID}/client-secret" \
    | jq -r '.value')
[[ -z "$SECRET" || "$SECRET" == "null" ]] && fail "Could not retrieve client secret"
echo "$SECRET" > /tmp/oo-client-secret.txt
log "Client secret saved to /tmp/oo-client-secret.txt"

# ── onlyoffice-mobile (public PKCE — iOS app) ─────────────────────────────────
create_client_if_missing "onlyoffice-mobile" '{
  "clientId": "onlyoffice-mobile",
  "name": "OnlyOffice Mobile (PKCE)",
  "enabled": true,
  "protocol": "openid-connect",
  "publicClient": true,
  "standardFlowEnabled": true,
  "directAccessGrantsEnabled": false,
  "redirectUris": ["'"${MOBILE_REDIRECT_URI}"'"],
  "webOrigins": ["+"],
  "attributes": {
    "pkce.code.challenge.method": "S256"
  }
}'

# ── Audience mapper: mobile token includes onlyoffice-client as audience ──────
MOBILE_INTERNAL_ID=$(kc_get "${KEYCLOAK_URL}/admin/realms/${REALM}/clients?clientId=onlyoffice-mobile" \
    | jq -r '.[0].id')

existing_mapper=$(kc_get "${KEYCLOAK_URL}/admin/realms/${REALM}/clients/${MOBILE_INTERNAL_ID}/protocol-mappers/models" \
    | jq -r '.[] | select(.name=="onlyoffice-audience") | .id // empty')

if [[ -n "$existing_mapper" ]]; then
    log "Audience mapper already exists — skipping."
else
    log "Adding audience mapper to onlyoffice-mobile ..."
    kc_post "${KEYCLOAK_URL}/admin/realms/${REALM}/clients/${MOBILE_INTERNAL_ID}/protocol-mappers/models" -d '{
      "name": "onlyoffice-audience",
      "protocol": "openid-connect",
      "protocolMapper": "oidc-audience-mapper",
      "consentRequired": false,
      "config": {
        "included.client.audience": "onlyoffice-client",
        "access.token.claim": "true",
        "id.token.claim": "false"
      }
    }'
    log "Audience mapper added."
fi

log "Keycloak realm '${REALM}' setup complete."
log "  Web client:    onlyoffice-client (secret in /tmp/oo-client-secret.txt)"
log "  Mobile client: onlyoffice-mobile (PKCE, redirect: ${MOBILE_REDIRECT_URI})"
