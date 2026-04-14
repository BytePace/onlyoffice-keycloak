#!/usr/bin/env bash
# setup-nginx.sh — Installs and configures nginx as reverse proxy.
# Expected env vars: APP_DOMAIN, AUTH_DOMAIN (optional),
#                    KEYCLOAK_MODE (new|existing), NGINX_CONF_TEMPLATE

set -uo pipefail

APP_DOMAIN="${APP_DOMAIN:?}"
KEYCLOAK_MODE="${KEYCLOAK_MODE:-existing}"
AUTH_DOMAIN="${AUTH_DOMAIN:-}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="${SCRIPT_DIR}/../nginx"
CONF_DEST="/etc/nginx/sites-available/onlyoffice-sso.conf"

log()  { echo "[setup-nginx] $*"; }
warn() { echo "[setup-nginx] WARN: $*" >&2; }
fail() { echo "[setup-nginx] ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Run as root"
command -v nginx >/dev/null 2>&1 || { apt-get update -q && apt-get install -y nginx; }

# Check if template exists
[[ -f "${TEMPLATE_DIR}/onlyoffice-sso.conf.template" ]] || fail "Template not found: ${TEMPLATE_DIR}/onlyoffice-sso.conf.template"

# Build auth server block
AUTH_BLOCK=""
if [[ "$KEYCLOAK_MODE" == "new" && -n "$AUTH_DOMAIN" ]]; then
    AUTH_BLOCK=$(sed "s|{AUTH_DOMAIN}|${AUTH_DOMAIN}|g" "${TEMPLATE_DIR}/keycloak-server-block.template")
fi

# Assemble main config
if ! sed \
    -e "s|{APP_DOMAIN}|${APP_DOMAIN}|g" \
    "${TEMPLATE_DIR}/onlyoffice-sso.conf.template" \
    | awk -v block="${AUTH_BLOCK}" '{gsub(/{AUTH_SERVER_BLOCK}/, block); print}' \
    > "$CONF_DEST"; then
    fail "Failed to generate nginx config"
fi

log "Nginx config written to ${CONF_DEST}"

# Enable site
ln -sf "$CONF_DEST" /etc/nginx/sites-enabled/onlyoffice-sso.conf

# Remove default site if present
rm -f /etc/nginx/sites-enabled/default

# Test and reload nginx
if ! nginx -t 2>&1; then
    fail "nginx config test failed"
fi

if ! systemctl reload nginx 2>&1; then
    fail "Failed to reload nginx"
fi

log "nginx reloaded successfully."

# Setup SSL with certbot if email provided
if [[ -n "$CERTBOT_EMAIL" ]] && ! [[ -d "/etc/letsencrypt/live/${APP_DOMAIN}" ]]; then
    log "Setting up SSL certificate for ${APP_DOMAIN}..."
    command -v certbot >/dev/null 2>&1 || apt-get install -y certbot python3-certbot-nginx

    if certbot certonly --nginx --non-interactive --agree-tos \
        -m "$CERTBOT_EMAIL" -d "$APP_DOMAIN" 2>&1; then
        log "SSL certificate obtained for ${APP_DOMAIN}"
        systemctl reload nginx
    else
        warn "Failed to obtain SSL certificate. Check certbot logs."
    fi
else
    [[ -d "/etc/letsencrypt/live/${APP_DOMAIN}" ]] && log "SSL certificate already exists for ${APP_DOMAIN}"
fi
