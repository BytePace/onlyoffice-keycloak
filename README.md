# Nextcloud + OnlyOffice Deployment

This stack deploys Nextcloud as the primary user cabinet (files/doc list) and integrates ONLYOFFICE Document Server for spreadsheet editing.

## What it deploys
- `nextcloud:29-apache`
- `mariadb:10.11`
- `redis:7-alpine`
- `onlyoffice/documentserver:latest`

## Quick start
```bash
cd /path/to/onlyoffice-keycloak
sudo bash deploy.sh \
  --domain sheets.bytepace.com \
  --certbot-email bytepace.sitgsa@gmail.com \
  --email-user noreply@example.com \
  --email-password 'SMTP_APP_PASSWORD' \
  --nextcloud-admin-user admin \
  --nextcloud-admin-password 'CHANGE_ME' \
  --keycloak-url https://auth.bytepace.com \
  --keycloak-realm ssa \
  --setup-nginx
```

To keep the contacts list visible in the top-right menu, pass:
```bash
sudo bash deploy.sh ... --show-contacts
```

## Rollback
```bash
sudo bash deploy.sh --rollback
sudo bash deploy.sh --rollback --delete-all
```

## Fresh Reset
```bash
sudo bash scripts/reset-fresh.sh
```
This removes the current Nextcloud/OnlyOffice/Keycloak deployment artifacts, nginx vhosts, and Let's Encrypt certificates for `sheets.bytepace.com` and `auth.bytepace.com`.

## Smoke User
Create a temporary verified Keycloak user for login testing:
```bash
bash scripts/manage-smoke-user.sh create \
  --keycloak-url https://auth.bytepace.com \
  --realm ssa \
  --keycloak-admin-password 'YOUR_ADMIN_PASSWORD'
```
Default smoke password: `SmokePass123!`

Delete the same user after testing:
```bash
bash scripts/manage-smoke-user.sh delete \
  --keycloak-url https://auth.bytepace.com \
  --realm ssa \
  --keycloak-admin-password 'YOUR_ADMIN_PASSWORD' \
  --email smoke-123456@bytepace.test
```

## Browser Smoke
Run a browser-level smoke test on a machine with Node.js and Playwright installed:
```bash
node scripts/browser-smoke.mjs \
  --base-url https://sheets.bytepace.com \
  --manage-smoke-user true \
  --keycloak-url https://auth.bytepace.com \
  --realm ssa \
  --keycloak-admin-password 'YOUR_ADMIN_PASSWORD' \
  --screenshot /tmp/onlyoffice-smoke.png
```
This script creates a temporary verified Keycloak user, logs in through Keycloak, waits for Nextcloud Files, creates a spreadsheet, verifies that the ONLYOFFICE editor opens, and then deletes the temporary user.

## Notes
- Nextcloud UI is served on `https://<domain>/`
- OnlyOffice Document Server is served on `https://<domain>/editor/`
- `onlyoffice` app is auto-installed and configured via `occ` (`richdocuments` is disabled)
- `user_oidc` is auto-installed and configured against Keycloak realm `ssa`
- OIDC login entrypoint: `https://<domain>/apps/user_oidc/login/1`
- Local login form is disabled and `/login` auto-redirects to Keycloak (`keycloak-ssa`)
- Contacts list is hidden by default (`contactsinteraction` disabled). Use `--show-contacts` to enable it.
- To configure mail for Keycloak password reset / verification, pass `--email-user`, `--email-password`, optionally `--email-host` and `--email-port`.
