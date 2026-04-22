#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

REMOTE_HOST="root@91.99.85.118"
REMOTE_REPO_PATH="/tmp/onlyoffice-keycloak"
APP_DOMAIN="sheets.bytepace.com"
AUTH_DOMAIN="auth.bytepace.com"
REALM="ssa"
CERTBOT_EMAIL=""
EMAIL_USER=""
EMAIL_PASSWORD=""
EMAIL_HOST="smtp.gmail.com"
EMAIL_PORT="587"
SCREENSHOT_PATH="/tmp/onlyoffice-smoke.png"
INSECURE=true

log() {
  echo "[smoke-fresh] $*"
}

fail() {
  echo "[smoke-fresh] ERROR: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  bash scripts/smoke-fresh.sh --certbot-email <email> --email-user <email> --email-password <password> [options]

Options:
  --remote-host <user@host>       Default: root@91.99.85.118
  --remote-repo-path <path>       Default: /tmp/onlyoffice-keycloak
  --domain <domain>               Default: sheets.bytepace.com
  --auth-domain <domain>          Default: auth.bytepace.com
  --realm <realm>                 Default: ssa
  --certbot-email <email>         Required
  --email-user <email>            Required
  --email-password <password>     Required (SMTP app password)
  --email-host <host>             Default: smtp.gmail.com
  --email-port <port>             Default: 587
  --screenshot <path>             Default: /tmp/onlyoffice-smoke.png
  --insecure <true|false>         Default: true
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote-host) REMOTE_HOST="$2"; shift 2 ;;
    --remote-repo-path) REMOTE_REPO_PATH="$2"; shift 2 ;;
    --domain) APP_DOMAIN="$2"; shift 2 ;;
    --auth-domain) AUTH_DOMAIN="$2"; shift 2 ;;
    --realm) REALM="$2"; shift 2 ;;
    --certbot-email) CERTBOT_EMAIL="$2"; shift 2 ;;
    --email-user) EMAIL_USER="$2"; shift 2 ;;
    --email-password) EMAIL_PASSWORD="$2"; shift 2 ;;
    --email-host) EMAIL_HOST="$2"; shift 2 ;;
    --email-port) EMAIL_PORT="$2"; shift 2 ;;
    --screenshot) SCREENSHOT_PATH="$2"; shift 2 ;;
    --insecure) INSECURE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; fail "Unknown option: $1" ;;
  esac
done

[[ -n "$CERTBOT_EMAIL" ]] || fail "--certbot-email is required"
[[ -n "$EMAIL_USER" ]] || fail "--email-user is required"
[[ -n "$EMAIL_PASSWORD" ]] || fail "--email-password is required"

log "Syncing local deploy scripts to ${REMOTE_HOST}:${REMOTE_REPO_PATH}"
scp "${REPO_ROOT}/deploy.sh" "${REMOTE_HOST}:${REMOTE_REPO_PATH}/deploy.sh"
scp "${REPO_ROOT}/scripts/reset-fresh.sh" "${REMOTE_HOST}:${REMOTE_REPO_PATH}/scripts/reset-fresh.sh"

log "Running fresh reset on VPS"
ssh "$REMOTE_HOST" "bash '${REMOTE_REPO_PATH}/scripts/reset-fresh.sh'"

log "Running deploy on VPS"
ssh "$REMOTE_HOST" \
  "cd '${REMOTE_REPO_PATH}' && ./deploy.sh \
  --domain '${APP_DOMAIN}' \
  --keycloak-mode new \
  --auth-domain '${AUTH_DOMAIN}' \
  --certbot-email '${CERTBOT_EMAIL}' \
  --email-user '${EMAIL_USER}' \
  --email-password '${EMAIL_PASSWORD}' \
  --email-host '${EMAIL_HOST}' \
  --email-port '${EMAIL_PORT}' \
  --setup-nginx"

log "Reading Keycloak admin password from VPS credentials"
KEYCLOAK_ADMIN_PASSWORD="$(ssh "$REMOTE_HOST" "awk -F': ' '/^Keycloak admin password:/ {print \$2}' /opt/nextcloud-onlyoffice/credentials.txt | tail -n1")"
[[ -n "$KEYCLOAK_ADMIN_PASSWORD" ]] || fail "Could not read Keycloak admin password from /opt/nextcloud-onlyoffice/credentials.txt"
NEXTCLOUD_ADMIN_USER="$(ssh "$REMOTE_HOST" "awk -F': ' '/^Nextcloud admin user:/ {print \$2}' /opt/nextcloud-onlyoffice/credentials.txt | tail -n1")"
NEXTCLOUD_ADMIN_PASSWORD="$(ssh "$REMOTE_HOST" "awk -F': ' '/^Nextcloud admin password:/ {print \$2}' /opt/nextcloud-onlyoffice/credentials.txt | tail -n1")"
[[ -n "$NEXTCLOUD_ADMIN_USER" && -n "$NEXTCLOUD_ADMIN_PASSWORD" ]] || fail "Could not read Nextcloud admin credentials from /opt/nextcloud-onlyoffice/credentials.txt"

log "Running browser smoke test locally"
node "${REPO_ROOT}/scripts/browser-smoke.mjs" \
  --base-url "https://${APP_DOMAIN}" \
  --manage-smoke-user true \
  --keycloak-url "https://${AUTH_DOMAIN}" \
  --realm "${REALM}" \
  --keycloak-admin-password "${KEYCLOAK_ADMIN_PASSWORD}" \
  --nextcloud-admin-user "${NEXTCLOUD_ADMIN_USER}" \
  --nextcloud-admin-password "${NEXTCLOUD_ADMIN_PASSWORD}" \
  --insecure "${INSECURE}" \
  --screenshot "${SCREENSHOT_PATH}"

log "Smoke fresh completed successfully. Screenshot: ${SCREENSHOT_PATH}"
