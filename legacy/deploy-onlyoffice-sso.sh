#!/usr/bin/env bash
# deploy.sh — One-command deployment of OnlyOffice + Keycloak SSO stack.
#
# Keycloak modes:
#   existing  — reuse an existing Keycloak; ensures realm "ssa" + OnlyOffice OIDC clients
#   new       — deploy a fresh Keycloak + PostgreSQL alongside the other services
#
# Usage (interactive):
#   sudo bash deploy.sh
#
# Usage (CLI / non-interactive):
#   # Existing Keycloak:
#   sudo bash deploy.sh \
#     --keycloak-mode existing \
#     --keycloak-url https://auth.example.com \
#     --keycloak-admin-password "secret" \
#     --app-domain app.example.com \
#     --certbot-email admin@example.com \
#     --email-user "noreply@example.com" \
#     --email-password "app-password" \
#     --setup-nginx
#
#   # New Keycloak (with SMTP):
#   sudo bash deploy.sh \
#     --keycloak-mode new \
#     --auth-domain auth.example.com \
#     --app-domain app.example.com \
#     --certbot-email admin@example.com \
#     --email-user "noreply@example.com" \
#     --email-password "app-password" \
#     --email-host "smtp.gmail.com" \
#     --email-port "587" \
#     --setup-nginx
#
# Rollback:
#   sudo bash deploy.sh --rollback [--delete-all] [--delete-realm]
#   (--delete-realm removes only OnlyOffice clients from realm ssa, not the whole realm)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="/opt/onlyoffice-sso"
ENV_FILE="${DEPLOY_DIR}/.env"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.yml"
LOG_FILE="${DEPLOY_DIR}/deploy.log"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()     { echo -e "${BLUE}[deploy]${NC} $*" | tee -a "${LOG_FILE:-/tmp/oo-deploy.log}"; }
success() { echo -e "${GREEN}[deploy]${NC} $*" | tee -a "${LOG_FILE:-/tmp/oo-deploy.log}"; }
warn()    { echo -e "${YELLOW}[deploy]${NC} $*" | tee -a "${LOG_FILE:-/tmp/oo-deploy.log}"; }
fail()    { echo -e "${RED}[deploy] ERROR:${NC} $*" | tee -a "${LOG_FILE:-/tmp/oo-deploy.log}" >&2; exit 1; }
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
    is_apt_based || fail "Cannot auto-install '$pkg': apt-get is not available on this OS"
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

    apt_update_once
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

has_keycloak_postgres_volume() {
    docker volume ls -q 2>/dev/null | grep -q 'oo-sso-keycloak-db'
}

reset_keycloak_postgres_volume() {
    log "Resetting Keycloak PostgreSQL volume ..."
    local vols

    if [[ -d "$DEPLOY_DIR" ]]; then
        cd "$DEPLOY_DIR"
        docker_compose --env-file .env down 2>/dev/null || docker_compose down 2>/dev/null || true
    fi

    vols=$(docker volume ls -q 2>/dev/null | grep 'oo-sso-keycloak-db' || true)
    if [[ -z "$vols" ]]; then
        warn "No Keycloak PostgreSQL volume found (nothing to reset)."
        return
    fi

    while IFS= read -r vol; do
        [[ -z "$vol" ]] && continue
        if ! docker volume rm "$vol" >/dev/null 2>&1; then
            fail "Could not remove Docker volume '$vol'. Stop related containers first."
        fi
    done <<< "$vols"

    success "Keycloak PostgreSQL volume reset complete."
}

ensure_xlsx_files() {
    log "Ensuring .xlsx files exist for existing documents ..."
    local out
    out=$(docker exec -i oo-sso-api python - <<'PY' 2>/tmp/oo-xlsx-migrate.err || true
from pathlib import Path
import openpyxl

docs_dir = Path("/data/docs")
created = 0

if docs_dir.exists():
    for meta_file in docs_dir.glob("*.meta.json"):
        doc_id = meta_file.name[:-10]  # strip ".meta.json"
        xlsx_path = docs_dir / f"{doc_id}.xlsx"
        if not xlsx_path.exists():
            wb = openpyxl.Workbook()
            wb.save(xlsx_path)
            created += 1

print(created)
PY
)

    if [[ "$out" =~ ^[0-9]+$ ]]; then
        success "Document file check complete. Missing .xlsx created: ${out}"
    else
        warn "Could not verify/create missing .xlsx files: $(cat /tmp/oo-xlsx-migrate.err 2>/dev/null || echo unknown error)"
    fi
}

# ── Defaults ──────────────────────────────────────────────────────────────────
KEYCLOAK_MODE=""         # existing | new
KEYCLOAK_URL=""          # used when mode=existing
KEYCLOAK_ADMIN_PASSWORD=""
AUTH_DOMAIN=""           # used when mode=new
APP_DOMAIN=""            # single domain for API and editor
CERTBOT_EMAIL=""
EMAIL_USER=""            # SMTP for Keycloak email (password reset, etc)
EMAIL_PASSWORD=""
EMAIL_HOST="smtp.gmail.com"
EMAIL_PORT="587"
MOBILE_REDIRECT_URI="com.bytepace.scan-it-to-google-sheets://oauth/callback"
SETUP_NGINX=false
ROLLBACK=false
DELETE_ALL=false
DELETE_REALM=false
KEEP_DATA=false
OO_CLIENT_SECRET=""
KEYCLOAK_REALM="ssa"
KEYCLOAK_VERSION="24.0"
POSTGRES_VERSION="15-alpine"
RESET_POSTGRES_VOLUME=false

# ── Argument parser ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --keycloak-mode)          KEYCLOAK_MODE="$2";            shift 2 ;;
        --keycloak-url)           KEYCLOAK_URL="$2";             shift 2 ;;
        --keycloak-admin-password) KEYCLOAK_ADMIN_PASSWORD="$2"; shift 2 ;;
        --auth-domain)            AUTH_DOMAIN="$2";              shift 2 ;;
        --app-domain)             APP_DOMAIN="$2";               shift 2 ;;
        --docs-domain)            APP_DOMAIN="$2";               shift 2 ;;
        --edit-domain)            shift 2 ;;
        --certbot-email)          CERTBOT_EMAIL="$2";            shift 2 ;;
        --email-user)             EMAIL_USER="$2";               shift 2 ;;
        --email-password)         EMAIL_PASSWORD="$2";           shift 2 ;;
        --email-host)             EMAIL_HOST="$2";               shift 2 ;;
        --email-port)             EMAIL_PORT="$2";               shift 2 ;;
        --mobile-redirect-uri)    MOBILE_REDIRECT_URI="$2";      shift 2 ;;
        --setup-nginx)            SETUP_NGINX=true;              shift   ;;
        --rollback)               ROLLBACK=true;                 shift   ;;
        --delete-all)             DELETE_ALL=true;               shift   ;;
        --delete-realm)           DELETE_REALM=true;             shift   ;;
        --keep-data)              KEEP_DATA=true;                shift   ;;
        --reset-postgres-volume)  RESET_POSTGRES_VOLUME=true;    shift   ;;
        *) fail "Unknown argument: $1" ;;
    esac
done

# ── Rollback ──────────────────────────────────────────────────────────────────
if [[ "$ROLLBACK" == true ]]; then
    log "Rolling back OnlyOffice SSO deployment ..."
    cd "${DEPLOY_DIR}" 2>/dev/null && docker_compose down || true

    if [[ "$DELETE_REALM" == true ]]; then
        log "Attempting to remove OnlyOffice OIDC clients from Keycloak realm '${KEYCLOAK_REALM:-ssa}' ..."

        # Read config from .env if not provided via CLI
        if [[ -z "$KEYCLOAK_URL" && -f "${ENV_FILE}" ]]; then
            KEYCLOAK_URL=$(grep "^KEYCLOAK_URL=" "${ENV_FILE}" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
            if [[ -z "$KEYCLOAK_URL" ]]; then
                KEYCLOAK_URL=$(grep "^KEYCLOAK_EXTERNAL_URL=" "${ENV_FILE}" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
            fi
        fi
        if [[ -z "$KEYCLOAK_ADMIN_PASSWORD" && -f "${ENV_FILE}" ]]; then
            KEYCLOAK_ADMIN_PASSWORD=$(grep "^KEYCLOAK_ADMIN_PASSWORD=" "${ENV_FILE}" | cut -d= -f2- | tr -d '"')
        fi
        if [[ -z "$KEYCLOAK_REALM" && -f "${ENV_FILE}" ]]; then
            KEYCLOAK_REALM=$(grep "^KEYCLOAK_REALM=" "${ENV_FILE}" 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
        fi
        KEYCLOAK_REALM="${KEYCLOAK_REALM:-ssa}"

        if [[ -n "$KEYCLOAK_URL" && -n "$KEYCLOAK_ADMIN_PASSWORD" ]]; then
            # Removes only onlyoffice-client / onlyoffice-mobile (shared realm preserved)
            if KEYCLOAK_URL="$KEYCLOAK_URL" \
               KEYCLOAK_ADMIN_PASSWORD="$KEYCLOAK_ADMIN_PASSWORD" \
               KEYCLOAK_REALM="$KEYCLOAK_REALM" \
               bash "${SCRIPT_DIR}/scripts/delete-realm.sh"; then
                success "OnlyOffice OIDC clients removed from realm '${KEYCLOAK_REALM}'"
            else
                warn "Could not remove OnlyOffice clients (check Keycloak connectivity)"
            fi
        else
            warn "Could not read Keycloak config for client deletion"
        fi
    fi

    if [[ "$DELETE_ALL" == true ]]; then
        docker volume rm oo-sso-api-data oo-sso-onlyoffice-data oo-sso-onlyoffice-logs \
                         oo-sso-keycloak-db 2>/dev/null || true
        rm -rf "${DEPLOY_DIR}"
        success "All data deleted."
    else
        success "Containers stopped. Data volumes preserved."
    fi
    exit 0
fi

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || fail "Run as root: sudo bash deploy.sh"

# ── Requirements ─────────────────────────────────────────────────────────────
ensure_base_dependencies
ensure_docker_engine

for cmd in docker curl openssl jq; do
    command -v "$cmd" >/dev/null 2>&1 || fail "Required tool not found after bootstrap: $cmd"
done
if ! command -v docker-compose >/dev/null 2>&1 && ! docker compose version >/dev/null 2>&1; then
    fail "docker-compose/docker compose not found after bootstrap"
fi

mkdir -p "${DEPLOY_DIR}"
touch "${LOG_FILE}"
log "Deployment started: $(date)"

# ── Interactive prompts (when running in a terminal without full CLI args) ────
if [[ -t 0 ]]; then
    if [[ -z "$KEYCLOAK_MODE" ]]; then
        echo ""
        echo "Keycloak setup:"
        echo "  [1] Use EXISTING Keycloak instance (realm 'ssa' + OnlyOffice clients)"
        echo "  [2] Deploy NEW Keycloak alongside OnlyOffice"
        read -rp "Choose [1/2]: " kc_choice
        case "$kc_choice" in
            1) KEYCLOAK_MODE="existing" ;;
            2) KEYCLOAK_MODE="new" ;;
            *) fail "Invalid choice" ;;
        esac
    fi

    if [[ "$KEYCLOAK_MODE" == "existing" ]]; then
        [[ -z "$KEYCLOAK_URL" ]] && \
            read -rp "Existing Keycloak URL (e.g. https://auth.example.com): " KEYCLOAK_URL
        [[ -z "$KEYCLOAK_ADMIN_PASSWORD" ]] && \
            read -rsp "Keycloak admin password: " KEYCLOAK_ADMIN_PASSWORD && echo
    else
        [[ -z "$AUTH_DOMAIN" ]] && \
            read -rp "New Keycloak domain (e.g. auth.example.com): " AUTH_DOMAIN
    fi

    [[ -z "$APP_DOMAIN" ]]    && read -rp "Application domain (e.g. app.example.com, API at /api, editor at /editor): " APP_DOMAIN
    [[ -z "$CERTBOT_EMAIL" ]] && read -rp "Email for Let's Encrypt certificates: " CERTBOT_EMAIL

    # Only ask about nginx if --setup-nginx was not provided
    if [[ "$SETUP_NGINX" != true ]]; then
        read -rp "Configure nginx reverse proxy? [y/N]: " nginx_yn
        [[ "$nginx_yn" =~ ^[Yy]$ ]] && SETUP_NGINX=true
    fi
fi

# ── Validate inputs ───────────────────────────────────────────────────────────
[[ -z "$KEYCLOAK_MODE" ]]    && fail "--keycloak-mode required (existing|new)"
[[ -z "$APP_DOMAIN" ]]       && fail "--app-domain required"

if [[ "$KEYCLOAK_MODE" == "existing" ]]; then
    [[ -z "$KEYCLOAK_URL" ]]            && fail "--keycloak-url required for mode=existing"
    [[ -z "$KEYCLOAK_ADMIN_PASSWORD" ]] && fail "--keycloak-admin-password required for mode=existing"
    KEYCLOAK_INTERNAL_URL="$KEYCLOAK_URL"
    KEYCLOAK_EXTERNAL_URL="$KEYCLOAK_URL"
else
    [[ -z "$AUTH_DOMAIN" ]] && fail "--auth-domain required for mode=new"
    KEYCLOAK_INTERNAL_URL="http://oo-sso-keycloak:8080"
    KEYCLOAK_EXTERNAL_URL="https://${AUTH_DOMAIN}"

    # For the realm setup script to reach Keycloak before nginx is up,
    # we use the local port
    KEYCLOAK_SETUP_URL="http://127.0.0.1:8090"
fi

# OIDC issuer set after secrets load (KEYCLOAK_REALM may come from .env)

# ── Load or generate secrets ──────────────────────────────────────────────────
if [[ "$KEYCLOAK_MODE" == "new" && "$RESET_POSTGRES_VOLUME" == true ]]; then
    reset_keycloak_postgres_volume
fi

EXISTING_KEYCLOAK_PG_VOLUME=false
if [[ "$KEYCLOAK_MODE" == "new" ]] && has_keycloak_postgres_volume; then
    EXISTING_KEYCLOAK_PG_VOLUME=true
fi

if [[ -f "$ENV_FILE" && "$KEEP_DATA" == true ]]; then
    log "Loading existing secrets from ${ENV_FILE} ..."
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    if [[ "$KEYCLOAK_MODE" == "new" && -z "${POSTGRES_KEYCLOAK_PASSWORD:-}" ]]; then
        fail "POSTGRES_KEYCLOAK_PASSWORD missing in ${ENV_FILE}. Fix .env or reset DB volume (--reset-postgres-volume)."
    fi
else
    if [[ "$KEYCLOAK_MODE" == "new" && "$EXISTING_KEYCLOAK_PG_VOLUME" == true && "$KEEP_DATA" == true ]]; then
        fail "Found existing Keycloak PostgreSQL volume but no reusable ${ENV_FILE}. Refusing to rotate DB password. Restore .env or rerun with --reset-postgres-volume."
    fi

    log "Generating secrets ..."
    ONLYOFFICE_JWT_SECRET="$(openssl rand -hex 32)"
    if [[ "$KEYCLOAK_MODE" == "new" ]]; then
        KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)}"
        POSTGRES_KEYCLOAK_PASSWORD="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 20)"
    fi
fi

KEYCLOAK_REALM="${KEYCLOAK_REALM:-ssa}"
OIDC_ISSUER_EXTERNAL="${KEYCLOAK_EXTERNAL_URL}/realms/${KEYCLOAK_REALM}"

# ── Copy API source to deploy dir ─────────────────────────────────────────────
log "Copying spreadsheet-api source to ${DEPLOY_DIR}/api/ ..."
rm -rf "${DEPLOY_DIR}/api"
cp -r "${SCRIPT_DIR}/api" "${DEPLOY_DIR}/api"

# ── Write .env ────────────────────────────────────────────────────────────────
log "Writing ${ENV_FILE} ..."
cat > "$ENV_FILE" <<EOF
KEYCLOAK_MODE=${KEYCLOAK_MODE}
KEYCLOAK_REALM=${KEYCLOAK_REALM}
KEYCLOAK_EXTERNAL_URL=${KEYCLOAK_EXTERNAL_URL}
OIDC_ISSUER_EXTERNAL=${OIDC_ISSUER_EXTERNAL}
APP_DOMAIN=${APP_DOMAIN}
ONLYOFFICE_JWT_SECRET=${ONLYOFFICE_JWT_SECRET}
MOBILE_REDIRECT_URI=${MOBILE_REDIRECT_URI}
OO_CLIENT_SECRET=${OO_CLIENT_SECRET}
EOF

if [[ "$KEYCLOAK_MODE" == "new" ]]; then
    cat >> "$ENV_FILE" <<EOF
AUTH_DOMAIN=${AUTH_DOMAIN}
KEYCLOAK_ADMIN_PASSWORD=${KEYCLOAK_ADMIN_PASSWORD}
POSTGRES_KEYCLOAK_PASSWORD=${POSTGRES_KEYCLOAK_PASSWORD}
KEYCLOAK_VERSION=${KEYCLOAK_VERSION}
POSTGRES_VERSION=${POSTGRES_VERSION}
EOF
fi
chmod 600 "$ENV_FILE"

# ── Generate docker-compose.yml ───────────────────────────────────────────────
log "Generating ${COMPOSE_FILE} ..."

# Common services block
COMPOSE_COMMON=$(cat <<YAML
  spreadsheet-api:
    build: ./api
    container_name: oo-sso-api
    restart: unless-stopped
    environment:
      KEYCLOAK_ISSUER: ${KEYCLOAK_INTERNAL_URL}/realms/${KEYCLOAK_REALM}
      KEYCLOAK_ISSUER_EXTERNAL: ${OIDC_ISSUER_EXTERNAL}
      OO_CLIENT_SECRET: ${OO_CLIENT_SECRET}
      ONLYOFFICE_JWT_SECRET: ${ONLYOFFICE_JWT_SECRET}
      ONLYOFFICE_DOCS_EXTERNAL_URL: https://${APP_DOMAIN}/editor
      API_EXTERNAL_URL: https://${APP_DOMAIN}/api
      DATA_DIR: /data
    volumes:
      - oo-sso-api-data:/data
    ports:
      - "127.0.0.1:8000:8000"
    networks:
      - oo-sso-net

  onlyoffice-docs:
    image: onlyoffice/documentserver:latest
    container_name: oo-sso-onlyoffice
    restart: unless-stopped
    environment:
      JWT_ENABLED: "true"
      JWT_SECRET: ${ONLYOFFICE_JWT_SECRET}
      JWT_HEADER: Authorization
      JWT_IN_BODY: "true"
    ports:
      - "127.0.0.1:8091:80"
    volumes:
      - oo-sso-onlyoffice-data:/var/www/onlyoffice/Data
      - oo-sso-onlyoffice-logs:/var/log/onlyoffice
    networks:
      - oo-sso-net
YAML
)

if [[ "$KEYCLOAK_MODE" == "new" ]]; then
    cat > "$COMPOSE_FILE" <<YAML
version: "3.8"

services:
  postgres-keycloak:
    image: postgres:${POSTGRES_VERSION}
    container_name: oo-sso-postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB: keycloak
      POSTGRES_USER: keycloak
      POSTGRES_PASSWORD: ${POSTGRES_KEYCLOAK_PASSWORD}
    volumes:
      - oo-sso-keycloak-db:/var/lib/postgresql/data
    networks:
      - oo-sso-net
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U keycloak -d keycloak"]
      interval: 10s
      timeout: 5s
      retries: 10

  keycloak:
    image: quay.io/keycloak/keycloak:${KEYCLOAK_VERSION}
    container_name: oo-sso-keycloak
    restart: unless-stopped
    command: start
    environment:
      KC_DB: postgres
      KC_DB_URL: jdbc:postgresql://postgres-keycloak:5432/keycloak
      KC_DB_USERNAME: keycloak
      KC_DB_PASSWORD: ${POSTGRES_KEYCLOAK_PASSWORD}
      KC_HOSTNAME: ${AUTH_DOMAIN}
      KC_HOSTNAME_STRICT: "false"
      KC_HTTP_ENABLED: "true"
      KC_PROXY: edge
      KEYCLOAK_ADMIN: admin
      KEYCLOAK_ADMIN_PASSWORD: ${KEYCLOAK_ADMIN_PASSWORD}
      JAVA_OPTS_APPEND: "-Xms256m -Xmx512m -XX:MetaspaceSize=96M -XX:MaxMetaspaceSize=256m"
    ports:
      - "127.0.0.1:8090:8080"
    depends_on:
      postgres-keycloak:
        condition: service_healthy
    networks:
      - oo-sso-net

${COMPOSE_COMMON}

volumes:
  oo-sso-keycloak-db:
  oo-sso-api-data:
  oo-sso-onlyoffice-data:
  oo-sso-onlyoffice-logs:

networks:
  oo-sso-net:
    driver: bridge
YAML
else
    cat > "$COMPOSE_FILE" <<YAML
version: "3.8"

services:
${COMPOSE_COMMON}

volumes:
  oo-sso-api-data:
  oo-sso-onlyoffice-data:
  oo-sso-onlyoffice-logs:

networks:
  oo-sso-net:
    driver: bridge
YAML
fi

# ── Start containers ──────────────────────────────────────────────────────────
cd "${DEPLOY_DIR}"
log "Starting containers (docker compose up --build) ..."

if [[ "$KEYCLOAK_MODE" == "new" ]]; then
    docker_compose --env-file .env up -d postgres-keycloak keycloak
    log "Waiting for Keycloak to start (up to 5 min) ..."
    keycloak_ready=false
    for _ in $(seq 1 60); do
        if docker_compose logs keycloak 2>/dev/null | grep -q "Running the server\|started in"; then
            keycloak_ready=true
            break
        fi
        sleep 5
    done
    if [[ "$keycloak_ready" != true ]]; then
        if docker_compose logs keycloak 2>/dev/null | grep -q 'password authentication failed'; then
            fail "Keycloak DB password mismatch detected. Restore ${ENV_FILE} or rerun with --reset-postgres-volume."
        fi
        fail "Keycloak did not become ready in time. Check logs: cd ${DEPLOY_DIR} && docker compose logs keycloak"
    fi
    success "Keycloak is running."
fi

docker_compose --env-file .env up -d --build spreadsheet-api onlyoffice-docs
log "Waiting for spreadsheet-api ..."
timeout 60 bash -c 'until curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; do sleep 3; done'
success "Spreadsheet API is running."

# Wait for OnlyOffice to initialize (check logs for completion markers)
log "Waiting for OnlyOffice Document Server to initialize (up to 3 min) ..."
timeout 180 bash -c '
until docker logs oo-sso-onlyoffice 2>&1 | grep -qE "Done|listening|ready"; do
  sleep 5
done
' && log "OnlyOffice initialization detected." || warn "OnlyOffice initialization check timed out (may still be initializing)."

# Additional wait for OnlyOffice port to be responsive
log "Waiting for OnlyOffice HTTP port to respond ..."
timeout 60 bash -c 'until curl -sf http://127.0.0.1:8091/healthcheck >/dev/null 2>&1; do sleep 2; done' \
  && success "OnlyOffice Document Server is running." \
  || warn "OnlyOffice healthcheck timeout (may still be initializing)."

# ── Configure Keycloak realm ──────────────────────────────────────────────────
log "Configuring Keycloak realm '${KEYCLOAK_REALM}' (OnlyOffice OIDC clients) ..."
SETUP_KC_URL="${KEYCLOAK_SETUP_URL:-$KEYCLOAK_URL}"
if ! KEYCLOAK_URL="$SETUP_KC_URL" \
KEYCLOAK_ADMIN_PASSWORD="$KEYCLOAK_ADMIN_PASSWORD" \
KEYCLOAK_REALM="$KEYCLOAK_REALM" \
APP_DOMAIN="$APP_DOMAIN" \
MOBILE_REDIRECT_URI="$MOBILE_REDIRECT_URI" \
EMAIL_USER="$EMAIL_USER" \
EMAIL_PASSWORD="$EMAIL_PASSWORD" \
EMAIL_HOST="$EMAIL_HOST" \
EMAIL_PORT="$EMAIL_PORT" \
    bash "${SCRIPT_DIR}/scripts/keycloak-realm-setup.sh"; then
    warn "Keycloak realm setup failed. Check /tmp/keycloak-setup.log for details."
    warn "Continuing with deployment..."
fi

# Retrieve and persist the client secret (if available)
if [[ -f /tmp/oo-client-secret.txt ]]; then
    OO_CLIENT_SECRET="$(cat /tmp/oo-client-secret.txt)"
    # Update .env file with the client secret (replace or add the line)
    if grep -q "^OO_CLIENT_SECRET=" "$ENV_FILE"; then
        sed -i "s|^OO_CLIENT_SECRET=.*|OO_CLIENT_SECRET=${OO_CLIENT_SECRET}|" "$ENV_FILE"
    else
        echo "OO_CLIENT_SECRET=${OO_CLIENT_SECRET}" >> "$ENV_FILE"
    fi
    rm -f /tmp/oo-client-secret.txt
    success "Keycloak realm configured."

    # Restart the API container to apply the new secret
    log "Restarting spreadsheet-api container with new client secret..."
    cd "${DEPLOY_DIR}"
    docker_compose --env-file .env up -d --no-build spreadsheet-api || warn "Failed to restart API container"
else
    warn "Client secret not found. Keycloak realm setup may have failed."
fi

# Upgrade metadata-only documents (from older versions) to real XLSX files
# so OnlyOffice can fetch them without "Download failed".
ensure_xlsx_files

# ── Setup nginx ───────────────────────────────────────────────────────────────
if [[ "$SETUP_NGINX" == true ]]; then
    log "Configuring nginx ..."
    if APP_DOMAIN="$APP_DOMAIN" \
    KEYCLOAK_MODE="$KEYCLOAK_MODE" \
    AUTH_DOMAIN="${AUTH_DOMAIN:-}" \
    CERTBOT_EMAIL="$CERTBOT_EMAIL" \
        bash "${SCRIPT_DIR}/legacy/setup-nginx-onlyoffice-sso.sh"; then
        success "nginx configured."
    else
        warn "nginx setup encountered issues. Check /var/log/nginx/error.log"
    fi
fi

# ── Run tests ─────────────────────────────────────────────────────────────────
log "Running deployment tests ..."
APP_DOMAIN="$APP_DOMAIN" \
KEYCLOAK_MODE="$KEYCLOAK_MODE" \
KEYCLOAK_REALM="$KEYCLOAK_REALM" \
KEYCLOAK_URL="${KEYCLOAK_SETUP_URL:-${KEYCLOAK_URL:-}}" \
AUTH_DOMAIN="${AUTH_DOMAIN:-}" \
    bash "${SCRIPT_DIR}/scripts/test-deployment.sh" || warn "Some tests failed — check above output."

# ── Write credentials & mobile config ────────────────────────────────────────
CREDS_FILE="${DEPLOY_DIR}/deploy-credentials.txt"
OUTPUT_FILE="${DEPLOY_DIR}/deploy-output.txt"

cat > "$CREDS_FILE" <<EOF
OnlyOffice SSO — Deployment Credentials
Generated: $(date)
========================================

Keycloak mode: ${KEYCLOAK_MODE}
EOF
if [[ "$KEYCLOAK_MODE" == "new" ]]; then
    cat >> "$CREDS_FILE" <<EOF
Keycloak admin URL:      https://${AUTH_DOMAIN}/admin
Keycloak admin user:     admin
Keycloak admin password: ${KEYCLOAK_ADMIN_PASSWORD}
PostgreSQL password:     ${POSTGRES_KEYCLOAK_PASSWORD}
EOF
else
    echo "Keycloak URL: ${KEYCLOAK_URL}" >> "$CREDS_FILE"
fi
cat >> "$CREDS_FILE" <<EOF

OIDC Issuer:             ${OIDC_ISSUER_EXTERNAL}
onlyoffice-client secret: ${OO_CLIENT_SECRET:-"(not configured - keycloak realm setup failed)"}
OnlyOffice JWT secret:   ${ONLYOFFICE_JWT_SECRET}
EOF
chmod 600 "$CREDS_FILE"

# Mobile JSON config (paste into remote /config endpoint)
MOBILE_CONFIG=$(cat <<JSON
{
  "onlyoffice": {
    "api_url": "https://${APP_DOMAIN}/api",
    "oidc_issuer": "${OIDC_ISSUER_EXTERNAL}",
    "client_id": "onlyoffice-mobile",
    "redirect_uri": "${MOBILE_REDIRECT_URI}"
  }
}
JSON
)

cat > "$OUTPUT_FILE" <<EOF
OnlyOffice SSO — Deployment Summary
Generated: $(date)
========================================

Spreadsheet API:  https://${APP_DOMAIN}/api
OnlyOffice editor: https://${APP_DOMAIN}/editor
EOF
[[ "$KEYCLOAK_MODE" == "new" ]] && echo "Keycloak:         https://${AUTH_DOMAIN}" >> "$OUTPUT_FILE"
cat >> "$OUTPUT_FILE" <<EOF

── Mobile app remote config (JSON) ──
${MOBILE_CONFIG}
EOF

success "=========================================="
success "Deployment complete!"
success "  API:    https://${APP_DOMAIN}/api"
success "  Editor: https://${APP_DOMAIN}/editor"
[[ "$KEYCLOAK_MODE" == "new" ]] && success "  Auth:   https://${AUTH_DOMAIN}"
success ""
success "Credentials saved to: ${CREDS_FILE}"
success "Mobile config saved to: ${OUTPUT_FILE}"
success "=========================================="
