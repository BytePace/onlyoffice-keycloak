# Nextcloud + OnlyOffice Deployment

This stack deploys Nextcloud as the primary user cabinet (files/doc list) and integrates ONLYOFFICE Document Server for spreadsheet editing.

## What it deploys
- `nextcloud:29-apache`
- `mariadb:10.11`
- `redis:7-alpine`
- `onlyoffice/documentserver:latest`
- `onlyoffice-keycloak` spreadsheet API (`/api`)

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
  --nextcloud-admin-user admin \
  --nextcloud-admin-password 'YOUR_NEXTCLOUD_ADMIN_PASSWORD' \
  --insecure true \
  --screenshot /tmp/onlyoffice-smoke.png
```
This script creates a temporary verified Keycloak user, logs in through Keycloak, waits for Nextcloud Files, creates a spreadsheet, verifies that the ONLYOFFICE editor opens, and then deletes the temporary user in Keycloak. If Nextcloud admin credentials are passed, it also removes the corresponding local Nextcloud OIDC user.

## Smoke Fresh
Run full fresh reset + deploy on VPS and then browser smoke locally in one command:
```bash
bash scripts/smoke-fresh.sh \
  --certbot-email ruslan.musagitov@gmail.com \
  --email-user bytepace.sitgsa@gmail.com \
  --email-password 'SMTP_APP_PASSWORD'
```
By default it targets `root@91.99.85.118`, deploys `sheets.bytepace.com` + `auth.bytepace.com`, reads the generated Keycloak admin password from `/opt/nextcloud-onlyoffice/credentials.txt`, and runs `scripts/browser-smoke.mjs` with managed smoke user.

## Notes
- Nextcloud UI is served on `https://<domain>/`
- OnlyOffice API is served on `https://<domain>/api`
- OnlyOffice Document Server is served on `https://<domain>/editor/`
- `onlyoffice` app is auto-installed and configured via `occ` (`richdocuments` is disabled)
- `user_oidc` is auto-installed and configured against Keycloak realm `ssa`
- OIDC login entrypoint: `https://<domain>/apps/user_oidc/login/1`
- Local login form is disabled and `/login` auto-redirects to Keycloak (`keycloak-ssa`)
- The **Contacts** app (`contacts`) is installed and enabled for address books and Teams/Circles management.
- The header **Search contacts** menu stays hidden by default (`contactsinteraction` disabled, `dav` system address book not exposed, `theming_customcss` hides `#contactsmenu` on NC 29). Use `--show-contacts` to show that menu again.
- User enumeration in sharing/Teams is disabled: partial search must not list all accounts; adding a user share requires the **full email** (`shareapi_restrict_user_enumeration_full_match_email=yes`, not userid-only).
- To configure mail for both Keycloak and Nextcloud, pass `--email-user`, `--email-password`, optionally `--email-host` and `--email-port`.
- iOS mobile config JSON is written to `/opt/nextcloud-onlyoffice/deploy-output.txt`.

## Sharing spreadsheets (iOS picker + `/api` list)

The API document list (`GET /api/orgs/.../workspaces`) is **not** the same as opening a public link (`https://<domain>/s/...`) from an email.

| How User1 shares | Visible to User2 in API / iOS picker? |
|------------------|----------------------------------------|
| Nextcloud **Share → user** (full email of User2, must exist in Keycloak/Nextcloud) | Yes (after sync on list) |
| `POST /api/docs/{doc_id}/share` on the API web UI | Yes |
| Public link or “send link by email” only (`/s/...`) | **No** — link works in browser, not in picker |

**Recommended:** User1 opens the file in Nextcloud Files → Share → invite **User2’s login email** (same as Keycloak, e.g. OIDC `mapping-uid=email`). User2 signs in at `https://<domain>/` via Keycloak, then opens the picker or `https://<domain>/api/`.

**Storage location:** New spreadsheets from the mobile app are created in the user's **Nextcloud Files root** by default (`NEXTCLOUD_FILES_DIR` empty). Set `NEXTCLOUD_FILES_DIR` to a folder name if you want to scope new files to a subfolder instead.

**Shared folders:** When User1 shares a folder with User2, Nextcloud mounts it as a top-level folder (e.g. `SSA Forms` or `SSA Forms (2)` if the name collides). The workspace list includes a `folders` array (received shares) so the iOS picker shows those mounts separately. Spreadsheets inside use `parent_path` such as `SSA Forms (2)/…`.

If `NEXTCLOUD_FILES_DIR` is set to a subfolder name and that path is already a received share mount, new files go to `NEXTCLOUD_OWN_FILES_DIR_FALLBACK` (default `SSA Forms (My files)`) so they stay in the recipient's own storage.

**User share (required for picker):** User1 → Share → **Share with users** → full email `user2@example.com` (not “Copy link” alone). If Nextcloud requires share acceptance, open [Pending shares](https://<domain>/apps/files/pendingshares) or reload the API/iOS picker (auto-accept after deploy).

**Troubleshooting:** As User2, open `https://<domain>/api/session-info` (while logged in). Check:
- `nextcloud_pending_shares` — shares waiting for acceptance (API auto-accepts on list)
- `nextcloud_shared_with_me_total` — accepted shares from OCS (0 = no user share to this account)
- `nextcloud_dav_workbooks` — xlsx files Nextcloud returns over WebDAV (includes subfolders and share mounts)
- `nextcloud_shared_folder_mounts` — received folder shares for the picker `folders[]` array
- `documents_in_api_list` — documents returned to the iOS picker after sync

If `nextcloud_dav_workbooks` is `0`, the API token cannot list Nextcloud files (re-login at `/api/oauth/login` so the token includes the `nextcloud` audience). If DAV count is correct but `documents_in_api_list` is lower, check `documents_in_storage` and re-open the picker.

**Two separate logins:** Signing in on `https://<domain>/` (Nextcloud) does **not** authenticate `https://<domain>/api/`. Open `https://<domain>/api/oauth/login` (or `/api/` → redirect) once; after success, `/api/session-info` works. A `502` on `/api/oauth/callback` usually means the API container cannot reach Keycloak — redeploy so `nc-api` has `extra_hosts` for your auth host and uses the internal token URL.
