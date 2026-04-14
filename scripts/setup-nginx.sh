#!/usr/bin/env bash
# setup-nginx.sh — Installs and configures nginx as reverse proxy.
# Expected env vars: APP_DOMAIN, AUTH_DOMAIN (optional),
#                    KEYCLOAK_MODE (new|existing), NGINX_CONF_TEMPLATE

set -euo pipefail

APP_DOMAIN="${APP_DOMAIN:?}"
KEYCLOAK_MODE="${KEYCLOAK_MODE:-existing}"
AUTH_DOMAIN="${AUTH_DOMAIN:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="${SCRIPT_DIR}/../nginx"
CONF_DEST="/etc/nginx/sites-available/onlyoffice-sso.conf"

log()  { echo "[setup-nginx] $*"; }
fail() { echo "[setup-nginx] ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Run as root"
command -v nginx >/dev/null 2>&1 || { apt-get update -q && apt-get install -y nginx; }

# Build auth server block
AUTH_BLOCK=""
if [[ "$KEYCLOAK_MODE" == "new" && -n "$AUTH_DOMAIN" ]]; then
    AUTH_BLOCK=$(sed "s|{AUTH_DOMAIN}|${AUTH_DOMAIN}|g" "${TEMPLATE_DIR}/keycloak-server-block.template")
fi

# Assemble main config
sed \
    -e "s|{APP_DOMAIN}|${APP_DOMAIN}|g" \
    "${TEMPLATE_DIR}/onlyoffice-sso.conf.template" \
    | awk -v block="${AUTH_BLOCK}" '{gsub(/{AUTH_SERVER_BLOCK}/, block); print}' \
    > "$CONF_DEST"

log "Nginx config written to ${CONF_DEST}"

# Enable site
ln -sf "$CONF_DEST" /etc/nginx/sites-enabled/onlyoffice-sso.conf

# Remove default site if present
rm -f /etc/nginx/sites-enabled/default

nginx -t || fail "nginx config test failed"
systemctl reload nginx
log "nginx reloaded."
