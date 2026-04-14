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

# Setup SSL with certbot FIRST (before nginx config with SSL)
if [[ -n "$CERTBOT_EMAIL" ]] && ! [[ -d "/etc/letsencrypt/live/${APP_DOMAIN}" ]]; then
    log "Setting up SSL certificate for ${APP_DOMAIN}..."
    command -v certbot >/dev/null 2>&1 || apt-get install -y certbot python3-certbot-nginx

    # Start nginx temporarily for certbot challenge
    log "Starting nginx for certificate validation..."

    # Create a simple HTTP-only config for certbot
    mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
    cat > /etc/nginx/sites-available/onlyoffice-sso-temp.conf <<'TEMP'
server {
    listen 80;
    server_name {APP_DOMAIN};
    location / {
        return 200 "OK";
    }
}
TEMP
    sed -i "s|{APP_DOMAIN}|${APP_DOMAIN}|g" /etc/nginx/sites-available/onlyoffice-sso-temp.conf
    ln -sf /etc/nginx/sites-available/onlyoffice-sso-temp.conf /etc/nginx/sites-enabled/onlyoffice-sso-temp.conf

    nginx -t && systemctl reload nginx || log "Failed to start nginx for certbot"

    if certbot certonly --standalone --non-interactive --agree-tos \
        -m "$CERTBOT_EMAIL" -d "$APP_DOMAIN" 2>&1; then
        log "SSL certificate obtained for ${APP_DOMAIN}"
    else
        warn "Failed to obtain SSL certificate. Continuing without SSL."
    fi

    rm -f /etc/nginx/sites-enabled/onlyoffice-sso-temp.conf
fi

# Assemble main config (with SSL if available)
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

# Remove default and temp sites
rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/onlyoffice-sso-temp.conf

# Test and reload nginx
if ! nginx -t 2>&1; then
    fail "nginx config test failed"
fi

if ! systemctl reload nginx 2>&1; then
    fail "Failed to reload nginx"
fi

log "nginx configured and reloaded successfully."

# Verify SSL certificate
if [[ -d "/etc/letsencrypt/live/${APP_DOMAIN}" ]]; then
    log "SSL certificate verified for ${APP_DOMAIN}"
else
    warn "SSL certificate not found for ${APP_DOMAIN} - using HTTP only"
fi
