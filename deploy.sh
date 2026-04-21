#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="/opt/nextcloud-onlyoffice"
ENV_FILE="${DEPLOY_DIR}/.env"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.yml"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log(){ echo -e "${BLUE}[nextcloud-deploy]${NC} $*"; }
success(){ echo -e "${GREEN}[nextcloud-deploy]${NC} $*"; }
warn(){ echo -e "${YELLOW}[nextcloud-deploy]${NC} $*"; }
fail(){ echo -e "${RED}[nextcloud-deploy] ERROR:${NC} $*" >&2; exit 1; }

APT_UPDATED=false

is_apt_based() {
  command -v apt-get >/dev/null 2>&1
}

apt_update_once() {
  if [[ "${APT_UPDATED}" == false ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    APT_UPDATED=true
  fi
}

apt_install_if_missing() {
  local pkg="$1"
  local cmd="${2:-$1}"
  if command -v "$cmd" >/dev/null 2>&1; then
    return 0
  fi
  if is_apt_based && dpkg -s "$pkg" >/dev/null 2>&1; then
    return 0
  fi
  is_apt_based || fail "Cannot auto-install '${pkg}': apt-get is not available on this OS"
  export DEBIAN_FRONTEND=noninteractive
  apt_update_once
  apt-get install -y "$pkg"
}

ensure_base_dependencies() {
  log "Checking base dependencies ..."
  apt_install_if_missing ca-certificates
  apt_install_if_missing curl
  apt_install_if_missing jq
  apt_install_if_missing openssl
  apt_install_if_missing gnupg gpg
  apt_install_if_missing lsb-release lsb_release
  apt_install_if_missing git
}

ensure_docker_engine() {
  if command -v docker >/dev/null 2>&1 && (command -v docker-compose >/dev/null 2>&1 || docker compose version >/dev/null 2>&1); then
    return 0
  fi
  is_apt_based || fail "Docker auto-install is supported only on apt-based systems"

  log "Docker / Compose not found. Installing Docker engine ..."
  export DEBIAN_FRONTEND=noninteractive
  apt_update_once
  apt-get install -y ca-certificates curl gnupg lsb-release

  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi

  local distro codename arch
  distro="$(
    source /etc/os-release
    echo "${ID}"
  )"
  codename="$(
    source /etc/os-release
    echo "${VERSION_CODENAME}"
  )"
  arch="$(dpkg --print-architecture)"

  if [[ "${distro}" != "ubuntu" && "${distro}" != "debian" ]]; then
    fail "Unsupported distro for Docker auto-install: ${distro}"
  fi

  cat > /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${distro} ${codename} stable
EOF

  APT_UPDATED=false
  apt_update_once
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker || true
}

docker_compose() {
  if command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    docker compose "$@"
  fi
}

keycloak_request() {
  local method="$1"
  local url="$2"
  shift 2

  local response
  if ! response=$(curl -fsS -X "$method" "$url" "$@"); then
    fail "Keycloak request failed: ${method} ${url}"
  fi

  printf '%s' "$response"
}

[[ $EUID -eq 0 ]] || fail "Run as root"
ensure_base_dependencies
ensure_docker_engine
for c in docker curl openssl jq; do command -v "$c" >/dev/null 2>&1 || fail "Missing $c after bootstrap"; done
command -v docker-compose >/dev/null 2>&1 || docker compose version >/dev/null 2>&1 || fail "docker compose not found after bootstrap"

APP_DOMAIN=""
CERTBOT_EMAIL=""
NEXTCLOUD_ADMIN_USER="admin"
NEXTCLOUD_ADMIN_PASSWORD=""
DB_PASSWORD=""
ONLYOFFICE_JWT_SECRET=""
KEYCLOAK_URL="https://auth.bytepace.com"
KEYCLOAK_REALM="ssa"
KEYCLOAK_ADMIN_PASSWORD=""
SETUP_NGINX=false
ROLLBACK=false
DELETE_ALL=false
SHOW_CONTACTS=false
NC_PORT="8082"
OO_PORT="8092"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain|--app-domain) APP_DOMAIN="$2"; shift 2 ;;
    --certbot-email) CERTBOT_EMAIL="$2"; shift 2 ;;
    --nextcloud-admin-user) NEXTCLOUD_ADMIN_USER="$2"; shift 2 ;;
    --nextcloud-admin-password) NEXTCLOUD_ADMIN_PASSWORD="$2"; shift 2 ;;
    --db-password) DB_PASSWORD="$2"; shift 2 ;;
    --jwt-secret) ONLYOFFICE_JWT_SECRET="$2"; shift 2 ;;
    --keycloak-url) KEYCLOAK_URL="$2"; shift 2 ;;
    --keycloak-realm) KEYCLOAK_REALM="$2"; shift 2 ;;
    --keycloak-admin-password) KEYCLOAK_ADMIN_PASSWORD="$2"; shift 2 ;;
    --show-contacts) SHOW_CONTACTS=true; shift ;;
    --setup-nginx) SETUP_NGINX=true; shift ;;
    --rollback) ROLLBACK=true; shift ;;
    --delete-all) DELETE_ALL=true; shift ;;
    *) fail "Unknown argument: $1" ;;
  esac
done

if [[ "$ROLLBACK" == true ]]; then
  log "Rollback nextcloud-onlyoffice stack"
  cd "$DEPLOY_DIR" 2>/dev/null && docker_compose down || true
  if [[ "$DELETE_ALL" == true ]]; then
    docker volume rm nc-db nc-nextcloud nc-redis nc-oo-data nc-oo-logs 2>/dev/null || true
    rm -rf "$DEPLOY_DIR"
    success "All data removed"
  else
    success "Containers stopped; data preserved"
  fi
  exit 0
fi

[[ -n "$APP_DOMAIN" ]] || fail "--domain is required"
[[ -n "$NEXTCLOUD_ADMIN_PASSWORD" ]] || NEXTCLOUD_ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 16)"
[[ -n "$DB_PASSWORD" ]] || DB_PASSWORD="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)"
[[ -n "$ONLYOFFICE_JWT_SECRET" ]] || ONLYOFFICE_JWT_SECRET="$(openssl rand -hex 32)"

mkdir -p "$DEPLOY_DIR"

cat > "$ENV_FILE" <<ENV
APP_DOMAIN=${APP_DOMAIN}
NEXTCLOUD_ADMIN_USER=${NEXTCLOUD_ADMIN_USER}
NEXTCLOUD_ADMIN_PASSWORD=${NEXTCLOUD_ADMIN_PASSWORD}
DB_PASSWORD=${DB_PASSWORD}
ONLYOFFICE_JWT_SECRET=${ONLYOFFICE_JWT_SECRET}
NC_PORT=${NC_PORT}
OO_PORT=${OO_PORT}
ENV
chmod 600 "$ENV_FILE"

cat > "$COMPOSE_FILE" <<'YAML'
services:
  db:
    image: mariadb:10.11
    container_name: nc-db
    restart: unless-stopped
    command: --transaction-isolation=READ-COMMITTED --binlog-format=ROW
    environment:
      MYSQL_DATABASE: nextcloud
      MYSQL_USER: nextcloud
      MYSQL_PASSWORD: ${DB_PASSWORD}
      MYSQL_ROOT_PASSWORD: ${DB_PASSWORD}
    volumes:
      - nc-db:/var/lib/mysql
    networks: [nc-net]

  redis:
    image: redis:7-alpine
    container_name: nc-redis
    restart: unless-stopped
    volumes:
      - nc-redis:/data
    networks: [nc-net]

  nextcloud:
    image: nextcloud:29-apache
    container_name: nc-app
    restart: unless-stopped
    depends_on: [db, redis]
    environment:
      MYSQL_HOST: db
      MYSQL_DATABASE: nextcloud
      MYSQL_USER: nextcloud
      MYSQL_PASSWORD: ${DB_PASSWORD}
      NEXTCLOUD_ADMIN_USER: ${NEXTCLOUD_ADMIN_USER}
      NEXTCLOUD_ADMIN_PASSWORD: ${NEXTCLOUD_ADMIN_PASSWORD}
      REDIS_HOST: redis
      OVERWRITEPROTOCOL: https
      OVERWRITEHOST: ${APP_DOMAIN}
      TRUSTED_PROXIES: 172.16.0.0/12
    ports:
      - "127.0.0.1:${NC_PORT}:80"
    volumes:
      - nc-nextcloud:/var/www/html
    networks: [nc-net]

  onlyoffice:
    image: onlyoffice/documentserver:latest
    container_name: nc-onlyoffice
    restart: unless-stopped
    environment:
      JWT_ENABLED: "true"
      JWT_SECRET: ${ONLYOFFICE_JWT_SECRET}
      JWT_HEADER: Authorization
      JWT_IN_BODY: "true"
    ports:
      - "127.0.0.1:${OO_PORT}:80"
    volumes:
      - nc-oo-data:/var/www/onlyoffice/Data
      - nc-oo-logs:/var/log/onlyoffice
    networks: [nc-net]

volumes:
  nc-db:
  nc-nextcloud:
  nc-redis:
  nc-oo-data:
  nc-oo-logs:

networks:
  nc-net:
    driver: bridge
YAML

cd "$DEPLOY_DIR"
log "Starting containers"
docker_compose --env-file "$ENV_FILE" up -d

log "Waiting for Nextcloud"
timeout 300 bash -c 'until curl -sf http://127.0.0.1:'"${NC_PORT}"'/status.php >/dev/null 2>&1; do sleep 5; done' || fail "Nextcloud not ready"

log "Waiting for OnlyOffice"
timeout 180 bash -c 'until curl -sf http://127.0.0.1:'"${OO_PORT}"'/healthcheck >/dev/null 2>&1; do sleep 3; done' || fail "OnlyOffice not ready"

log "Configuring Nextcloud ONLYOFFICE app"
docker exec --user www-data nc-app php occ app:install onlyoffice >/dev/null 2>&1 || true
docker exec --user www-data nc-app php occ app:enable onlyoffice >/dev/null 2>&1 || true
docker exec --user www-data nc-app php occ app:disable richdocuments >/dev/null 2>&1 || true

docker exec --user www-data nc-app php occ config:app:set onlyoffice DocumentServerUrl --value="https://${APP_DOMAIN}/editor/" >/dev/null
docker exec --user www-data nc-app php occ config:app:set onlyoffice DocumentServerInternalUrl --value="http://nc-onlyoffice/" >/dev/null
docker exec --user www-data nc-app php occ config:app:set onlyoffice StorageUrl --value="https://${APP_DOMAIN}/" >/dev/null
docker exec --user www-data nc-app php occ config:app:set onlyoffice jwt_secret --value="${ONLYOFFICE_JWT_SECRET}" >/dev/null
docker exec --user www-data nc-app php occ config:app:set files_sharing shareapi_allow_share_dialog_user_enumeration --value=no >/dev/null
if [[ "$SHOW_CONTACTS" == true ]]; then
  docker exec --user www-data nc-app php occ app:enable contactsinteraction >/dev/null 2>&1 || true
else
  docker exec --user www-data nc-app php occ app:disable contactsinteraction >/dev/null 2>&1 || true
fi

docker exec --user www-data nc-app php occ config:system:set trusted_domains 0 --value="${APP_DOMAIN}" >/dev/null
docker exec --user www-data nc-app php occ config:system:set defaultapp --value="files" >/dev/null

auth_ip=$(hostname -I | awk '{print $1}')
[[ -n "$auth_ip" ]] && docker exec --user www-data nc-app php occ config:system:set trusted_proxies 0 --value="$auth_ip" >/dev/null || true

log "Configuring Nextcloud OIDC with Keycloak (${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM})"
if [[ -z "$KEYCLOAK_ADMIN_PASSWORD" && -f /opt/grist-sso/.env ]]; then
  KEYCLOAK_ADMIN_PASSWORD="$(grep '^KEYCLOAK_ADMIN_PASSWORD=' /opt/grist-sso/.env | cut -d= -f2- || true)"
fi
[[ -n "$KEYCLOAK_ADMIN_PASSWORD" ]] || fail "Keycloak admin password missing (pass --keycloak-admin-password or provide /opt/grist-sso/.env)"

KC_TOKEN_RESPONSE=$(keycloak_request POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "client_id=admin-cli" \
  --data-urlencode "grant_type=password" \
  --data-urlencode "username=admin" \
  --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
)
KC_TOKEN=$(printf '%s' "$KC_TOKEN_RESPONSE" | jq -r '.access_token // empty')
[[ -n "$KC_TOKEN" ]] || fail "Could not obtain Keycloak admin token: $(printf '%s' "$KC_TOKEN_RESPONSE" | jq -r '.error_description // .error // "empty response"' 2>/dev/null)"

KC_CLIENT_ID="nextcloud"
KC_CLIENTS_RESPONSE=$(keycloak_request GET "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/clients?clientId=${KC_CLIENT_ID}" \
  -H "Authorization: Bearer ${KC_TOKEN}")
KC_CLIENT_UUID=$(printf '%s' "$KC_CLIENTS_RESPONSE" | jq -r '.[0].id // empty')
if [[ -z "$KC_CLIENT_UUID" ]]; then
  keycloak_request POST "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/clients" \
    -H "Authorization: Bearer ${KC_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"clientId\":\"${KC_CLIENT_ID}\",\"name\":\"Nextcloud OIDC\",\"enabled\":true,\"protocol\":\"openid-connect\",\"publicClient\":false,\"standardFlowEnabled\":true,\"directAccessGrantsEnabled\":false,\"serviceAccountsEnabled\":false,\"redirectUris\":[\"https://${APP_DOMAIN}/apps/user_oidc/code\",\"https://${APP_DOMAIN}/apps/user_oidc/*\"],\"webOrigins\":[\"https://${APP_DOMAIN}\"],\"attributes\":{\"post.logout.redirect.uris\":\"https://${APP_DOMAIN}/*\"}}" >/dev/null
  KC_CLIENTS_RESPONSE=$(keycloak_request GET "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/clients?clientId=${KC_CLIENT_ID}" \
    -H "Authorization: Bearer ${KC_TOKEN}")
  KC_CLIENT_UUID=$(printf '%s' "$KC_CLIENTS_RESPONSE" | jq -r '.[0].id // empty')
fi
[[ -n "$KC_CLIENT_UUID" ]] || fail "Could not resolve Keycloak client id for '${KC_CLIENT_ID}'"

KC_CLIENT_RESPONSE=$(keycloak_request GET "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/clients/${KC_CLIENT_UUID}" \
  -H "Authorization: Bearer ${KC_TOKEN}")
KC_UPDATED=$(printf '%s' "$KC_CLIENT_RESPONSE" | jq --arg d "${APP_DOMAIN}" '
      .redirectUris=["https://\($d)/apps/user_oidc/code","https://\($d)/apps/user_oidc/*"]
      | .webOrigins=["https://\($d)"]
      | .attributes["post.logout.redirect.uris"]="https://\($d)/*"
      | .standardFlowEnabled=true
      | .publicClient=false
      | .directAccessGrantsEnabled=false
  ')
keycloak_request PUT "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/clients/${KC_CLIENT_UUID}" \
  -H "Authorization: Bearer ${KC_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "${KC_UPDATED}" >/dev/null

KC_CLIENT_SECRET_RESPONSE=$(keycloak_request GET "${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/clients/${KC_CLIENT_UUID}/client-secret" \
  -H "Authorization: Bearer ${KC_TOKEN}")
KC_CLIENT_SECRET=$(printf '%s' "$KC_CLIENT_SECRET_RESPONSE" | jq -r '.value // empty')
[[ -n "$KC_CLIENT_SECRET" ]] || fail "Could not obtain nextcloud client secret from Keycloak"

docker exec --user www-data nc-app php occ app:install user_oidc >/dev/null 2>&1 || true
docker exec --user www-data nc-app php occ app:enable user_oidc >/dev/null 2>&1 || true
docker exec --user www-data nc-app php occ user_oidc:provider keycloak-ssa \
  --clientid="${KC_CLIENT_ID}" \
  --clientsecret="${KC_CLIENT_SECRET}" \
  --discoveryuri="${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/.well-known/openid-configuration" \
  --endsessionendpointuri="${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/logout" \
  --postlogouturi="https://${APP_DOMAIN}/" \
  --scope="openid email profile" \
  --mapping-uid="email" \
  --mapping-display-name="name" \
  --mapping-email="email" \
  --check-bearer=1 \
  --bearer-provisioning=1 \
  --send-id-token-hint=1 >/dev/null
docker exec --user www-data nc-app php occ config:app:set user_oidc allow_multiple_user_backends --value=0 >/dev/null
docker exec --user www-data nc-app php occ config:system:set hide_login_form --type=boolean --value=true >/dev/null
success "Nextcloud OIDC provider configured (keycloak-ssa)"

if [[ "$SETUP_NGINX" == true ]]; then
  log "Configuring nginx"
  APP_DOMAIN="$APP_DOMAIN" CERTBOT_EMAIL="$CERTBOT_EMAIL" NC_PORT="$NC_PORT" OO_PORT="$OO_PORT" bash "$SCRIPT_DIR/scripts/setup-nginx-nextcloud.sh"
fi

cat > "${DEPLOY_DIR}/credentials.txt" <<CREDS
Nextcloud + OnlyOffice deployment
Generated: $(date)

URL: https://${APP_DOMAIN}
Nextcloud admin user: ${NEXTCLOUD_ADMIN_USER}
Nextcloud admin password: ${NEXTCLOUD_ADMIN_PASSWORD}
OnlyOffice JWT secret: ${ONLYOFFICE_JWT_SECRET}
Keycloak OIDC issuer: ${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}
Keycloak OIDC login URL: https://${APP_DOMAIN}/apps/user_oidc/login/1
CREDS
chmod 600 "${DEPLOY_DIR}/credentials.txt"

success "Deployment completed"
success "Nextcloud: https://${APP_DOMAIN}"
success "OnlyOffice endpoint: https://${APP_DOMAIN}/editor/"
success "Credentials: ${DEPLOY_DIR}/credentials.txt"
