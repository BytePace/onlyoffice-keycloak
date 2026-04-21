# Nextcloud + OnlyOffice Deployment

This stack deploys Nextcloud as the primary user cabinet (files/doc list) and integrates ONLYOFFICE Document Server for spreadsheet editing.

## What it deploys
- `nextcloud:29-apache`
- `mariadb:10.11`
- `redis:7-alpine`
- `onlyoffice/documentserver:latest`

## Quick start
```bash
cd nextcloud
sudo bash deploy.sh \
  --domain sheets.bytepace.com \
  --certbot-email bytepace.sitgsa@gmail.com \
  --nextcloud-admin-user admin \
  --nextcloud-admin-password 'CHANGE_ME' \
  --keycloak-url https://auth.bytepace.com \
  --keycloak-realm ssa \
  --setup-nginx
```

## Rollback
```bash
sudo bash deploy.sh --rollback
sudo bash deploy.sh --rollback --delete-all
```

## Notes
- Nextcloud UI is served on `https://<domain>/`
- OnlyOffice Document Server is served on `https://<domain>/editor/`
- `onlyoffice` app is auto-installed and configured via `occ` (`richdocuments` is disabled)
- `user_oidc` is auto-installed and configured against Keycloak realm `ssa`
- OIDC login entrypoint: `https://<domain>/apps/user_oidc/login/1`
- Local login form is disabled and `/login` auto-redirects to Keycloak (`keycloak-ssa`)
