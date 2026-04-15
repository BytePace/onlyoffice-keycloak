#!/usr/bin/env bash
# test-deployment.sh — Comprehensive deployment tests and diagnostics

set -uo pipefail

APP_DOMAIN="${APP_DOMAIN:?}"
KEYCLOAK_MODE="${KEYCLOAK_MODE:-existing}"
KEYCLOAK_URL="${KEYCLOAK_URL:-}"
AUTH_DOMAIN="${AUTH_DOMAIN:-}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-ssa}"

PASS=0; FAIL=0; WARN=0

ok()   { echo "  [PASS] $*"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }

check_url() {
    local label="$1" url="$2"
    local response=$(curl -s --max-time 10 -w "%{http_code}" -o /dev/null -k "$url" 2>/dev/null || echo "000")
    if [[ "$response" =~ ^[2] ]]; then
        ok "$label (HTTP $response)"
    else
        fail "$label (HTTP $response: $url)"
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

check_container() {
    local cname="$1"
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${cname}$"; then
        ok "Container running: $cname"
    else
        fail "Container not running: $cname"
    fi
}

echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║         OnlyOffice SSO Deployment Tests & Diagnostics          ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# DNS Resolution
echo "▶ DNS Resolution"
check_dns "$APP_DOMAIN"
[[ "$KEYCLOAK_MODE" == "new" && -n "$AUTH_DOMAIN" ]] && check_dns "$AUTH_DOMAIN"

# Docker containers
echo ""
echo "▶ Docker Containers"
check_container "oo-sso-api"
check_container "oo-sso-onlyoffice"
[[ "$KEYCLOAK_MODE" == "new" ]] && check_container "oo-sso-keycloak"
[[ "$KEYCLOAK_MODE" == "new" ]] && check_container "oo-sso-postgres"

# Local endpoints (localhost)
echo ""
echo "▶ Local Endpoints (127.0.0.1)"
check_url "API (localhost)"     "http://127.0.0.1:8000/health"
check_url "OnlyOffice (localhost)" "http://127.0.0.1:8091/healthcheck"

# Domain endpoints (HTTP)
echo ""
echo "▶ Domain Endpoints (HTTP)"
check_url "API via domain (HTTP)"     "http://${APP_DOMAIN}/api/health"
check_url "OnlyOffice via domain (HTTP)" "http://${APP_DOMAIN}/editor/healthcheck"

# Domain endpoints (HTTPS)
echo ""
echo "▶ Domain Endpoints (HTTPS)"
check_url "API via domain (HTTPS)"     "https://${APP_DOMAIN}/api/health"
check_url "OnlyOffice via domain (HTTPS)" "https://${APP_DOMAIN}/editor/healthcheck"

# Keycloak
echo ""
echo "▶ Keycloak"
if [[ "$KEYCLOAK_MODE" == "new" && -n "$AUTH_DOMAIN" ]]; then
    check_url "Keycloak OIDC discovery" "https://${AUTH_DOMAIN}/realms/${KEYCLOAK_REALM}/.well-known/openid-configuration"
elif [[ -n "$KEYCLOAK_URL" ]]; then
    check_url "Keycloak OIDC discovery (existing)" "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/.well-known/openid-configuration"
fi

# SSL Certificates
echo ""
echo "▶ SSL Certificates"
if [[ -d "/etc/letsencrypt/live/${APP_DOMAIN}" ]]; then
    ok "SSL cert for ${APP_DOMAIN}"
else
    warn "No SSL cert for ${APP_DOMAIN} (using HTTP)"
fi

# Container logs
echo ""
echo "▶ Container Logs (last 5 lines)"
echo "  API Container:"
docker logs --tail 5 oo-sso-api 2>/dev/null | sed 's/^/    /' || echo "    (no logs)"
echo "  OnlyOffice Container:"
docker logs --tail 5 oo-sso-onlyoffice 2>/dev/null | sed 's/^/    /' || echo "    (no logs)"

# nginx configuration
echo ""
echo "▶ Nginx Configuration"
if [[ -f "/etc/nginx/sites-available/onlyoffice-sso.conf" ]]; then
    ok "Nginx config exists"
    if nginx -t 2>&1 | grep -q "successful"; then
        ok "Nginx config is valid"
    else
        fail "Nginx config has errors"
        nginx -t 2>&1 | sed 's/^/    /'
    fi
else
    fail "Nginx config not found"
fi

# Environment variables
echo ""
echo "▶ Environment Variables (oo-sso-api)"
docker exec oo-sso-api env 2>/dev/null | grep -E "KEYCLOAK|ONLYOFFICE|API" | sed 's/^/  /' || warn "Could not read env vars"

# Summary
echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
TOTAL=$((PASS + FAIL + WARN))
echo "║ Results: $PASS PASS, $FAIL FAIL, $WARN WARN (Total: $TOTAL)              ║"
if [[ $FAIL -eq 0 ]]; then
    echo "║ ✓ Deployment successful!                                          ║"
else
    echo "║ ✗ Some tests failed - see details above                          ║"
fi
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

[[ $FAIL -eq 0 ]]
