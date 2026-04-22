#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="/opt/nextcloud-onlyoffice"
REPO_DEPLOY_SH="/tmp/onlyoffice-keycloak/deploy.sh"

log() { echo "[reset-fresh] $*"; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || {
  echo "[reset-fresh] ERROR: run as root" >&2
  exit 1
}

log "Stopping current deployment if present"
if [[ -f "$REPO_DEPLOY_SH" ]]; then
  bash "$REPO_DEPLOY_SH" --rollback --delete-all || true
fi

log "Removing known containers"
for container in nc-app nc-db nc-redis nc-onlyoffice nc-keycloak nc-postgres-keycloak nc-api; do
  docker rm -f "$container" 2>/dev/null || true
done

log "Removing known networks"
for network in nextcloud-onlyoffice_nc-net; do
  docker network rm "$network" 2>/dev/null || true
done

log "Removing known volumes"
for volume in \
  nextcloud-onlyoffice_nc-db \
  nextcloud-onlyoffice_nc-nextcloud \
  nextcloud-onlyoffice_nc-redis \
  nextcloud-onlyoffice_nc-oo-data \
  nextcloud-onlyoffice_nc-oo-logs \
  nextcloud-onlyoffice_nc-keycloak-db \
  nextcloud-onlyoffice_nc-api-data \
  nc-db \
  nc-nextcloud \
  nc-redis \
  nc-oo-data \
  nc-oo-logs \
  nc-keycloak-db \
  nc-api-data; do
  docker volume rm "$volume" 2>/dev/null || true
done

log "Removing deployment directory"
rm -rf "$DEPLOY_DIR"

log "Removing nginx vhosts"
rm -f \
  /etc/nginx/sites-enabled/nextcloud-onlyoffice.conf \
  /etc/nginx/sites-available/nextcloud-onlyoffice.conf \
  /etc/nginx/sites-enabled/nextcloud-temp.conf \
  /etc/nginx/sites-available/nextcloud-temp.conf

log "Removing Let's Encrypt certificates"
certbot delete --cert-name sheets.bytepace.com --non-interactive 2>/dev/null || true
certbot delete --cert-name auth.bytepace.com --non-interactive 2>/dev/null || true

rm -rf /etc/letsencrypt/live/sheets.bytepace.com
rm -rf /etc/letsencrypt/archive/sheets.bytepace.com
rm -rf /etc/letsencrypt/renewal/sheets.bytepace.com.conf

rm -rf /etc/letsencrypt/live/auth.bytepace.com
rm -rf /etc/letsencrypt/archive/auth.bytepace.com
rm -rf /etc/letsencrypt/renewal/auth.bytepace.com.conf

log "Reloading nginx if config is still valid"
nginx -t >/dev/null 2>&1 && systemctl reload nginx || true

log "Fresh reset complete"
