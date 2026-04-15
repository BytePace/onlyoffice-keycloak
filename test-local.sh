#!/usr/bin/env bash
# test-local.sh — Easy way to test OnlyOffice integration from your local machine
#
# Usage:
#   bash test-local.sh
#
# Or with custom credentials:
#   bash test-local.sh \
#     --keycloak-url https://auth.example.com \
#     --app-domain sheets.example.com \
#     --client-secret "your-secret" \
#     --test-user user@example.com \
#     --test-password "password"

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Defaults (change these to match your environment) ──────────────────────────
KEYCLOAK_URL="${KEYCLOAK_URL:-https://auth.bytepace.com}"
APP_DOMAIN="${APP_DOMAIN:-sheets.bytepace.com}"
CLIENT_SECRET="${CLIENT_SECRET:-2KO2YVLLZTSgvVUNxQbKpi2zawid996V}"
TEST_USER="${TEST_USER:-ruslan.musagitov@gmail.com}"
TEST_PASSWORD="${TEST_PASSWORD:-}"
REALM="ssa"

# ── Parse CLI arguments (override defaults) ───────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --keycloak-url)     KEYCLOAK_URL="$2"; shift 2 ;;
        --app-domain)       APP_DOMAIN="$2"; shift 2 ;;
        --client-secret)    CLIENT_SECRET="$2"; shift 2 ;;
        --test-user)        TEST_USER="$2"; shift 2 ;;
        --test-password)    TEST_PASSWORD="$2"; shift 2 ;;
        *)
            echo "Unknown argument: $1"
            exit 1
            ;;
    esac
done

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ── Check if password is set ──────────────────────────────────────────────────
if [[ -z "$TEST_PASSWORD" ]]; then
    echo -e "${YELLOW}⚠${NC} TEST_PASSWORD not set"
    echo ""
    echo "Set it with one of these methods:"
    echo ""
    echo "1. Direct command:"
    echo "   TEST_PASSWORD='your-password' bash test-local.sh"
    echo ""
    echo "2. Interactive:"
    read -sp "Enter password for $TEST_USER: " TEST_PASSWORD
    echo ""
fi

# ── Run integration test ──────────────────────────────────────────────────────
bash "${SCRIPT_DIR}/scripts/test-integration.sh" \
    --keycloak-url "$KEYCLOAK_URL" \
    --app-domain "$APP_DOMAIN" \
    --realm "$REALM" \
    --client-secret "$CLIENT_SECRET" \
    --test-user "$TEST_USER" \
    --test-password "$TEST_PASSWORD"
