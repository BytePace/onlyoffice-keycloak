#!/usr/bin/env bash
# test-deployment.sh — Basic smoke tests after deployment.

set -uo pipefail

DOCS_DOMAIN="${DOCS_DOMAIN:?}"
EDIT_DOMAIN="${EDIT_DOMAIN:?}"
KEYCLOAK_MODE="${KEYCLOAK_MODE:-existing}"
KEYCLOAK_URL="${KEYCLOAK_URL:-}"
AUTH_DOMAIN="${AUTH_DOMAIN:-}"

PASS=0; FAIL=0

ok()   { echo "  [PASS] $*"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

check_url() {
    local label="$1" url="$2"
    if curl -sf --max-time 10 -k "$url" >/dev/null 2>&1; then
        ok "$label ($url)"
    else
        fail "$label ($url)"
    fi
}

check_dns() {
    local domain="$1"
    if host "$domain" >/dev/null 2>&1 || nslookup "$domain" >/dev/null 2>&1; then
        ok "DNS: $domain"
    else
        fail "DNS: $domain"
    fi
}

echo ""
echo "=== OnlyOffice SSO Deployment Tests ==="
echo ""

# DNS
check_dns "$DOCS_DOMAIN"
check_dns "$EDIT_DOMAIN"
[[ "$KEYCLOAK_MODE" == "new" && -n "$AUTH_DOMAIN" ]] && check_dns "$AUTH_DOMAIN"

# Docker containers
echo ""
echo "--- Docker containers ---"
for cname in oo-sso-api oo-sso-onlyoffice; do
    if docker ps --format '{{.Names}}' | grep -q "^${cname}$"; then
        ok "Container running: $cname"
    else
        fail "Container not running: $cname"
    fi
done
[[ "$KEYCLOAK_MODE" == "new" ]] && for cname in oo-sso-keycloak oo-sso-postgres; do
    if docker ps --format '{{.Names}}' | grep -q "^${cname}$"; then
        ok "Container running: $cname"
    else
        fail "Container not running: $cname"
    fi
done

# HTTP checks
echo ""
echo "--- HTTP endpoints ---"
check_url "Spreadsheet API health"         "https://${DOCS_DOMAIN}/health"
check_url "OnlyOffice Docs"                "https://${EDIT_DOMAIN}/healthcheck"

if [[ "$KEYCLOAK_MODE" == "new" && -n "$AUTH_DOMAIN" ]]; then
    check_url "Keycloak OIDC discovery" "https://${AUTH_DOMAIN}/realms/onlyoffice/.well-known/openid-configuration"
elif [[ -n "$KEYCLOAK_URL" ]]; then
    check_url "Keycloak OIDC discovery (existing)" "${KEYCLOAK_URL}/realms/onlyoffice/.well-known/openid-configuration"
fi

# Summary
echo ""
echo "======================================="
TOTAL=$((PASS + FAIL))
echo "Results: ${PASS}/${TOTAL} passed"
[[ $FAIL -eq 0 ]] && echo "All tests passed." || echo "${FAIL} test(s) failed."
echo ""
[[ $FAIL -eq 0 ]]
