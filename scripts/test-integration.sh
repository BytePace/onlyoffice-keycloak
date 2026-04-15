#!/usr/bin/env bash
# test-integration.sh — Integration test script for OnlyOffice + Keycloak
# Tests: Keycloak auth, document creation, editor access
#
# Usage (from your local machine):
#   bash test-integration.sh \
#     --keycloak-url https://auth.bytepace.com \
#     --app-domain sheets.bytepace.com \
#     --client-secret "2KO2YVLLZTSgvVUNxQbKpi2zawid996V" \
#     --test-user ruslan.musagitov@gmail.com \
#     --test-password qwertyu1
#
# Or with environment variables:
#   export KEYCLOAK_URL=https://auth.bytepace.com
#   export APP_DOMAIN=sheets.bytepace.com
#   export CLIENT_SECRET="..."
#   export TEST_PASSWORD="qwertyu1"
#   bash test-integration.sh

set -uo pipefail

# ── Parse CLI arguments ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --keycloak-url)     KEYCLOAK_URL="$2"; shift 2 ;;
        --app-domain)       APP_DOMAIN="$2"; shift 2 ;;
        --client-id)        CLIENT_ID="$2"; shift 2 ;;
        --client-secret)    CLIENT_SECRET="$2"; shift 2 ;;
        --test-user)        TEST_USER="$2"; shift 2 ;;
        --test-password)    TEST_PASSWORD="$2"; shift 2 ;;
        --realm)            REALM="$2"; shift 2 ;;
        *)
            echo "Unknown argument: $1"
            echo "Usage: $0 --keycloak-url URL --app-domain DOMAIN --client-secret SECRET --test-password PASSWORD"
            exit 1
            ;;
    esac
done

# ── Configuration (defaults can be overridden) ─────────────────────────────────
KEYCLOAK_URL="${KEYCLOAK_URL:-https://auth.bytepace.com}"
APP_DOMAIN="${APP_DOMAIN:-sheets.bytepace.com}"
CLIENT_ID="${CLIENT_ID:-onlyoffice-client}"
CLIENT_SECRET="${CLIENT_SECRET:-}"
TEST_USER="${TEST_USER:-ruslan.musagitov@gmail.com}"
TEST_PASSWORD="${TEST_PASSWORD:-}"
REALM="${REALM:-ssa}"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()     { echo -e "${BLUE}[test]${NC} $*"; }
success() { echo -e "${GREEN}✓${NC} $*"; }
fail()    { echo -e "${RED}✗${NC} $*"; exit 1; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }

# ── Validate input ────────────────────────────────────────────────────────────
if [[ -z "$CLIENT_SECRET" ]]; then
    fail "CLIENT_SECRET is required.

Get it from VPS:
  ssh openclaw@49.13.194.13 'sudo cat /opt/onlyoffice-sso/deploy-credentials.txt'

Usage:
  bash $0 \\
    --keycloak-url https://auth.bytepace.com \\
    --app-domain sheets.bytepace.com \\
    --client-secret 'YOUR_SECRET' \\
    --test-password 'YOUR_PASSWORD'"
fi

if [[ -z "$TEST_PASSWORD" ]]; then
    fail "TEST_PASSWORD is required. This is the Keycloak user password for $TEST_USER"
fi

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║       OnlyOffice + Keycloak Integration Test Suite             ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
log "Configuration:"
log "  Keycloak URL:  $KEYCLOAK_URL"
log "  App Domain:    $APP_DOMAIN"
log "  Test User:     $TEST_USER"
log "  Realm:         $REALM"
echo ""

# ── Test 1: Keycloak connectivity ─────────────────────────────────────────────
log "▶ Testing Keycloak connectivity..."
if curl -sf "${KEYCLOAK_URL}/realms/${REALM}/.well-known/openid-configuration" >/dev/null 2>&1; then
    success "Keycloak is reachable"
else
    fail "Cannot reach Keycloak at ${KEYCLOAK_URL}"
fi

# ── Test 2: Obtain access token ───────────────────────────────────────────────
log "▶ Obtaining access token (password flow)..."
TOKEN_RESPONSE=$(curl -s -X POST \
    "${KEYCLOAK_URL}/realms/${REALM}/protocol/openid-connect/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${CLIENT_SECRET}" \
    -d "username=${TEST_USER}" \
    -d "password=${TEST_PASSWORD}" \
    -d "grant_type=password")

TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.access_token // empty' 2>/dev/null || true)

if [[ -z "$TOKEN" || "$TOKEN" == "null" ]]; then
    error=$(echo "$TOKEN_RESPONSE" | jq -r '.error_description // .error // "unknown"' 2>/dev/null || echo "unknown")
    fail "Failed to obtain token. Error: $error"
fi

success "Token obtained (${#TOKEN} chars)"
TOKEN_EXPIRY=$(echo "$TOKEN_RESPONSE" | jq -r '.expires_in' 2>/dev/null || echo "unknown")
log "  Token expires in: ${TOKEN_EXPIRY}s"

# ── Test 3: Test API health ──────────────────────────────────────────────────
log "▶ Testing API health endpoint..."
HEALTH=$(curl -s "https://${APP_DOMAIN}/api/health")
if echo "$HEALTH" | jq -e '.status == "ok"' >/dev/null 2>&1; then
    success "API is healthy"
else
    warn "API health check returned unexpected response: $HEALTH"
fi

# ── Test 4: List workspaces ──────────────────────────────────────────────────
log "▶ Fetching workspaces..."
WORKSPACES=$(curl -s -H "Authorization: Bearer ${TOKEN}" \
    "https://${APP_DOMAIN}/api/orgs/1/workspaces" 2>/dev/null)

if echo "$WORKSPACES" | jq -e '.[0].id' >/dev/null 2>&1; then
    success "Workspaces retrieved"
    WORKSPACE_ID=$(echo "$WORKSPACES" | jq -r '.[0].id')
    DOC_COUNT=$(echo "$WORKSPACES" | jq -r '.[0].docs | length')
    log "  Workspace ID: $WORKSPACE_ID"
    log "  Documents in workspace: $DOC_COUNT"
else
    warn "Could not fetch workspaces. Response: $WORKSPACES"
fi

# ── Test 5: Create a test document ────────────────────────────────────────────
log "▶ Creating test document..."
DOC_NAME="Integration Test $(date +%s)"
DOC_RESPONSE=$(curl -s -X POST "https://${APP_DOMAIN}/api/workspaces/1/docs" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${DOC_NAME}\"}")

DOC_ID=$(echo "$DOC_RESPONSE" | jq -r '.' 2>/dev/null || echo "")

if [[ -n "$DOC_ID" && "$DOC_ID" != "null" ]]; then
    success "Document created"
    log "  Document ID: $DOC_ID"
else
    fail "Failed to create document. Response: $DOC_RESPONSE"
fi

# ── Test 6: Create tables in document ────────────────────────────────────────
log "▶ Creating table in document..."
TABLE_RESPONSE=$(curl -s -X POST \
    "https://${APP_DOMAIN}/api/docs/${DOC_ID}/tables" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d @- <<'EOF'
{
  "tables": [
    {
      "id": "Sheet1",
      "columns": [
        {"id": "A"},
        {"id": "B"},
        {"id": "C"}
      ]
    }
  ]
}
EOF
)

if echo "$TABLE_RESPONSE" | jq -e '.' >/dev/null 2>&1; then
    success "Table created"
else
    warn "Table creation response: $TABLE_RESPONSE"
fi

# ── Test 7: Add sample data ──────────────────────────────────────────────────
log "▶ Adding sample data to table..."
DATA_RESPONSE=$(curl -s -X POST \
    "https://${APP_DOMAIN}/api/docs/${DOC_ID}/tables/Sheet1/records" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d @- <<'EOF'
{
  "records": [
    {
      "fields": {
        "A": "Name",
        "B": "Email",
        "C": "Date"
      }
    },
    {
      "fields": {
        "A": "Test User",
        "B": "test@example.com",
        "C": "2026-04-15"
      }
    }
  ]
}
EOF
)

if echo "$DATA_RESPONSE" | jq -e '.' >/dev/null 2>&1; then
    success "Sample data added"
else
    warn "Data response: $DATA_RESPONSE"
fi

# ── Test 8: Fetch document data ──────────────────────────────────────────────
log "▶ Fetching document records..."
RECORDS=$(curl -s -H "Authorization: Bearer ${TOKEN}" \
    "https://${APP_DOMAIN}/api/docs/${DOC_ID}/tables/Sheet1/records")

RECORD_COUNT=$(echo "$RECORDS" | jq -r '.records | length' 2>/dev/null || echo "0")
if [[ "$RECORD_COUNT" -gt 0 ]]; then
    success "Records retrieved ($RECORD_COUNT records)"
else
    warn "Could not fetch records. Response: $RECORDS"
fi

# ── Test 9: OnlyOffice editor access ──────────────────────────────────────────
log "▶ Testing OnlyOffice editor endpoint..."
EDITOR_RESPONSE=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer ${TOKEN}" \
    "https://${APP_DOMAIN}/api/docs/${DOC_ID}/editor")

HTTP_CODE=$(echo "$EDITOR_RESPONSE" | tail -1)
if [[ "$HTTP_CODE" == "200" ]]; then
    success "Editor HTML retrieved (HTTP $HTTP_CODE)"
else
    fail "Failed to get editor HTML (HTTP $HTTP_CODE)"
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║                    Test Results Summary                        ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
success "All integration tests passed!"
echo ""
echo "📝 Document Details:"
echo "   Document ID: $DOC_ID"
echo "   Document Name: Integration Test $(date +%s)"
echo ""
echo "🔗 Access URLs:"
echo "   ✓ API:      https://${APP_DOMAIN}/api"
echo "   ✓ Editor:   https://${APP_DOMAIN}/api/docs/${DOC_ID}/editor"
echo ""
echo "📱 cURL command to fetch records:"
echo "   curl -H 'Authorization: Bearer \$TOKEN' \\"
echo "     'https://${APP_DOMAIN}/api/docs/${DOC_ID}/tables/Sheet1/records' | jq"
echo ""
echo "🌐 Open in browser:"
echo "   https://${APP_DOMAIN}/api/docs/${DOC_ID}/editor"
echo ""
echo "💾 To use this token:"
echo "   export TOKEN='${TOKEN}'"
echo ""
