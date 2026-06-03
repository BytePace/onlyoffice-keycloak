#!/usr/bin/env bash
set -euo pipefail

APP_DOMAIN="${APP_DOMAIN:?}"
AUTH_DOMAIN="${AUTH_DOMAIN:-}"
KEYCLOAK_MODE="${KEYCLOAK_MODE:-existing}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
NC_PORT="${NC_PORT:-8082}"
OO_PORT="${OO_PORT:-8092}"
KC_PORT="${KC_PORT:-8090}"
API_PORT="${API_PORT:-8088}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/../nginx/nextcloud-onlyoffice.conf.template"
KEYCLOAK_TEMPLATE="${SCRIPT_DIR}/../nginx/keycloak-server-block.template"
CONF_DEST="/etc/nginx/sites-available/nextcloud-onlyoffice.conf"

log(){ echo "[nextcloud-nginx] $*"; }
warn(){ echo "[nextcloud-nginx] WARN: $*" >&2; }
fail(){ echo "[nextcloud-nginx] ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Run as root"
command -v nginx >/dev/null 2>&1 || { apt-get update -q && apt-get install -y nginx; }

AUTH_BLOCK=""
if [[ "$KEYCLOAK_MODE" == "new" ]]; then
  [[ -n "$AUTH_DOMAIN" ]] || fail "AUTH_DOMAIN is required when KEYCLOAK_MODE=new"
  [[ -f "$KEYCLOAK_TEMPLATE" ]] || fail "Template not found: $KEYCLOAK_TEMPLATE"
  AUTH_BLOCK=$(sed \
    -e "s|{AUTH_DOMAIN}|${AUTH_DOMAIN}|g" \
    -e "s|127.0.0.1:8090|127.0.0.1:${KC_PORT}|g" \
    "$KEYCLOAK_TEMPLATE")
fi

need_app_cert=false
need_auth_cert=false
[[ ! -d "/etc/letsencrypt/live/${APP_DOMAIN}" ]] && need_app_cert=true
[[ "$KEYCLOAK_MODE" == "new" && ! -d "/etc/letsencrypt/live/${AUTH_DOMAIN}" ]] && need_auth_cert=true

if [[ -n "$CERTBOT_EMAIL" ]] && ([[ "$need_app_cert" == true ]] || [[ "$need_auth_cert" == true ]]); then
  command -v certbot >/dev/null 2>&1 || apt-get install -y certbot python3-certbot-nginx
  WEBROOT="/var/www/letsencrypt"
  mkdir -p "$WEBROOT"
  rm -f /etc/nginx/sites-enabled/nextcloud-onlyoffice.conf
  cat > /etc/nginx/sites-available/nextcloud-temp.conf <<TMP
server {
  listen 80;
  server_name ${APP_DOMAIN};
  root ${WEBROOT};
  location /.well-known/acme-challenge/ { allow all; }
  location / { return 301 http://\$host\$request_uri; }
}
TMP
  if [[ "$KEYCLOAK_MODE" == "new" ]]; then
    cat >> /etc/nginx/sites-available/nextcloud-temp.conf <<TMP
server {
  listen 80;
  server_name ${AUTH_DOMAIN};
  root ${WEBROOT};
  location /.well-known/acme-challenge/ { allow all; }
  location / { return 301 http://\$host\$request_uri; }
}
TMP
  fi
  ln -sf /etc/nginx/sites-available/nextcloud-temp.conf /etc/nginx/sites-enabled/nextcloud-temp.conf
  nginx -t && systemctl reload nginx

  if [[ "$need_app_cert" == true ]]; then
    certbot certonly --webroot -w "$WEBROOT" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" -d "$APP_DOMAIN" \
      || warn "Certbot failed for ${APP_DOMAIN}; continuing (HTTP-only may be used)"
  fi

  if [[ "$need_auth_cert" == true ]]; then
    certbot certonly --webroot -w "$WEBROOT" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" -d "$AUTH_DOMAIN" \
      || warn "Certbot failed for ${AUTH_DOMAIN}; continuing (HTTP-only may be used)"
  fi

  rm -f /etc/nginx/sites-enabled/nextcloud-temp.conf
fi

[[ -f "$TEMPLATE" ]] || fail "Template not found: $TEMPLATE"

sed \
  -e "s|{APP_DOMAIN}|${APP_DOMAIN}|g" \
  -e "s|{NC_PORT}|${NC_PORT}|g" \
  -e "s|{OO_PORT}|${OO_PORT}|g" \
  -e "s|{API_PORT}|${API_PORT}|g" \
  "$TEMPLATE" \
  | awk -v block="$AUTH_BLOCK" '{gsub(/{AUTH_SERVER_BLOCK}/, block); print}' \
  > "$CONF_DEST"

ln -sf "$CONF_DEST" /etc/nginx/sites-enabled/nextcloud-onlyoffice.conf
# Disable old onlyoffice vhost on same domain if present
rm -f /etc/nginx/sites-enabled/onlyoffice-sso.conf || true

nginx -t || fail "nginx config test failed"
systemctl reload nginx || fail "nginx reload failed"

log "nginx configured for ${APP_DOMAIN}"
