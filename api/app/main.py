import logging
import asyncio
import os
import secrets
import base64
import hashlib
import tempfile
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from fastapi import Depends, HTTPException, Request, Cookie
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi import FastAPI

from .auth import (
    get_current_user,
    resolve_access_token,
    _fetch_jwks,
    KEYCLOAK_ISSUER as KEYCLOAK_ISSUER_INTERNAL,
    KEYCLOAK_ISSUER_EXTERNAL,
)
from .session_store import SESSION_COOKIE, delete_session, load_session, save_tokens
from .models import AddRecordsRequest, CreateDocRequest, CreateTablesRequest, ShareDocRequest
from jose import jwt
from . import (
    access_requests,
    access_request_ui,
    editor_session,
    mailer,
    nextcloud,
    onlyoffice,
    spreadsheet,
    storage,
)

app = FastAPI(title="OnlyOffice Spreadsheet API", version="1.0.0")
logger = logging.getLogger(__name__)
_doc_async_locks: dict[str, asyncio.Lock] = {}

API_EXTERNAL_URL = os.getenv("API_EXTERNAL_URL", "").rstrip("/")
API_INTERNAL_URL = os.getenv("API_INTERNAL_URL", "http://api:8000").rstrip("/")
EDITOR_MODE = os.getenv("EDITOR_MODE", "embedded").strip().lower()
# Browser redirects use the public issuer; server-side token exchange uses KEYCLOAK_ISSUER (often internal).
KEYCLOAK_AUTH_BASE = (KEYCLOAK_ISSUER_EXTERNAL or KEYCLOAK_ISSUER_INTERNAL or "").rstrip("/")
KEYCLOAK_TOKEN_BASE = (KEYCLOAK_ISSUER_INTERNAL or KEYCLOAK_ISSUER_EXTERNAL or "").rstrip("/")
CLIENT_ID = "onlyoffice-client"
CLIENT_SECRET = os.getenv("OO_CLIENT_SECRET", "")

# Scoped to /api so JWT/session cookies are not sent to Nextcloud (/) on the same host.
API_COOKIE_PATH = "/api"

_API_COOKIE_NAMES = (
    "access_token",
    "id_token",
    SESSION_COOKIE,
    "pkce_verifier",
    "oauth_state",
    "oauth_doc_id",
    "oauth_redirect_to",
)


def _cookie_secure(request: Request) -> bool:
    """Respect reverse-proxy scheme; secure cookies only on HTTPS."""
    xf_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    scheme = xf_proto or request.url.scheme
    return scheme == "https"


def _clear_api_cookies(response: Response) -> None:
    """Remove API auth cookies (current path and legacy path=/)."""
    for name in _API_COOKIE_NAMES:
        response.delete_cookie(name, path=API_COOKIE_PATH)
        response.delete_cookie(name, path="/")


def _api_public_base() -> str:
    return (API_EXTERNAL_URL or API_INTERNAL_URL).rstrip("/")


def _access_request_review_url(token: str) -> str:
    return f"{_api_public_base()}/access-requests/{quote(token, safe='')}"


async def _optional_current_user(request: Request) -> dict | None:
    try:
        return await get_current_user(request)
    except HTTPException:
        return None


def _is_document_owner(meta: dict, user: dict) -> bool:
    return storage.get_doc_role(meta, user) == "owner"


def _user_email(user: dict) -> str:
    """Primary account label for ownership; prefers verified email from the token."""
    email = (user.get("email") or "").strip().lower()
    if email:
        return email
    preferred = (user.get("preferred_username") or "").strip().lower()
    if "@" in preferred:
        return preferred
    if preferred:
        return preferred
    return (user.get("sub") or "").strip().lower()


def _require_doc_access(meta: dict | None, user: dict, write: bool = False) -> str:
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")
    email = _user_email(user)
    has_access = storage.can_write(meta, user) if write else storage.can_read(meta, user)
    if not has_access:
        raise HTTPException(status_code=403, detail="Access denied")
    return email


async def _require_doc_access_with_nextcloud_id(
    meta: dict | None,
    user: dict,
    access_token: str,
    write: bool = False,
) -> dict:
    user_ctx = await _user_with_nextcloud_id(user, access_token)
    _require_doc_access(meta, user_ctx, write=write)
    return user_ctx


def _doc_nextcloud_path(meta: dict | None) -> str:
    return ((meta or {}).get("nextcloud_path") or "").strip()


async def _browser_open_url(meta: dict, access_token: str) -> str:
    """URL that opens the document in Nextcloud Files (same flow as the web UI)."""
    file_id = (meta.get("nextcloud_file_id") or "").strip()
    if not file_id:
        nextcloud_path = _doc_nextcloud_path(meta)
        if not nextcloud_path:
            raise HTTPException(status_code=404, detail="Document storage path is missing")
        file_id = await nextcloud.resolve_file_id(nextcloud_path, access_token)
        meta["nextcloud_file_id"] = file_id
        storage.save_document_meta(meta["id"], meta)
    return nextcloud.browser_open_url(file_id, _doc_nextcloud_path(meta))


def _request_access_token(
    request: Request,
    query_token: str | None = None,
) -> str:
    token = resolve_access_token(request)
    if token:
        return token
    query_value = (query_token or "").strip()
    if query_value:
        return query_value
    raise HTTPException(status_code=401, detail="Access token is required")


def _nextcloud_token_for_editor(
    request: Request,
    doc_id: str,
    *,
    editor_session_id: str = "",
    access_token: str = "",
) -> str:
    """Resolve Nextcloud token for browser or Document Server editor callbacks."""
    session = editor_session.load_editor_session(editor_session_id)
    if session and session.get("doc_id") == doc_id:
        token = (session.get("access_token") or "").strip()
        if token:
            return token
    return _request_access_token(request, access_token or None)


def _document_server_api_base() -> str:
    """
    Base URL for file/callback endpoints embedded in the OnlyOffice editor config.

    Document Server fetches the spreadsheet from this URL. Private Docker hostnames
    (http://nc-api:8000) are blocked by default (ALLOW_PRIVATE_IP_ADDRESS=false),
    so prefer the public /api URL when configured.
    """
    if API_EXTERNAL_URL:
        return API_EXTERNAL_URL
    return API_INTERNAL_URL


def _build_editor_config(
    meta: dict,
    user: dict,
    doc_id: str,
    editor_session_id: str,
    revision: int,
) -> dict:
    title = (meta.get("title") or meta.get("name") or "Spreadsheet").strip() or "Spreadsheet"
    qs = urlencode({"editor_session": editor_session_id})
    api_base = _document_server_api_base()
    file_url = f"{api_base}/docs/{quote(doc_id, safe='')}/file.xlsx?{qs}"
    callback_url = f"{api_base}/docs/{quote(doc_id, safe='')}/callback?{qs}"
    return onlyoffice.build_editor_config(
        doc_id=doc_id,
        title=title,
        user_email=_user_email(user),
        file_url=file_url,
        callback_url=callback_url,
        revision=revision,
    )


async def _with_doc_workbook(
    doc_id: str,
    access_token: str,
    handler,
    upload: bool,
    *,
    bump_revision: bool = False,
) -> object:
    meta = storage.get_document_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")

    nextcloud_path = _doc_nextcloud_path(meta)
    if not nextcloud_path:
        raise HTTPException(status_code=404, detail="Document storage path is missing")
    file_id = (meta.get("nextcloud_file_id") or "").strip()
    lock = _doc_async_locks.setdefault(doc_id, asyncio.Lock())
    async with lock:
        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = Path(tmpdir) / f"{doc_id}.xlsx"
            local_path.write_bytes(
                await nextcloud.download_bytes(nextcloud_path, access_token, file_id=file_id)
            )

            result = handler(local_path)

            if upload:
                await nextcloud.upload_bytes(
                    nextcloud_path,
                    local_path.read_bytes(),
                    access_token,
                    file_id=file_id,
                )
                if bump_revision:
                    try:
                        storage.bump_content_revision(doc_id)
                    except FileNotFoundError:
                        pass

            return result


# ── Custom exception handler for 401/403 on editor endpoints ──────────────────
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Redirect to OAuth login if accessing editor without auth"""
    # Redirect on 401 or 403 for editor endpoints
    if exc.status_code in [401, 403] and "/docs/" in request.url.path and "/editor" in request.url.path:
        # Extract doc_id from path
        path_parts = request.url.path.split("/")
        doc_id = ""
        if "docs" in path_parts:
            idx = path_parts.index("docs")
            if idx + 1 < len(path_parts):
                doc_id = path_parts[idx + 1]

        return RedirectResponse(
            url=f"/api/oauth/login?doc_id={doc_id}",
            status_code=302
        )

    # REST clients under /api expect JSON, not HTML error pages.
    accept = request.headers.get("accept", "")
    if request.url.path.startswith("/api/") or "application/json" in accept.lower():
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    return HTMLResponse(
        f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>",
        status_code=exc.status_code
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/session-info")
async def session_info(request: Request):
    """
    Diagnostics: which account the API sees vs what Nextcloud returns for shared_with_me.
    Requires a separate /api/ login (Nextcloud SSO does not set API cookies).
    """
    token = resolve_access_token(request) or ""
    if not token:
        return RedirectResponse(
            url="/api/oauth/login?redirect_to=/api/session-info",
            status_code=302,
        )

    user = await get_current_user(request)
    access_token = _request_access_token(request)
    user_ctx = await _user_with_nextcloud_id(user, access_token)
    nc_profile: dict = {}
    nc_share_count = 0
    nc_dav_count = 0
    nc_folder_mount_count = 0
    nc_pending_count = 0
    nc_pending_accepted = 0
    nc_pending_accept_errors: list[str] = []
    nc_shared_with_me_total = 0
    nc_pending_samples: list[dict] = []
    nc_shared_with_me_samples: list[dict] = []
    nc_error = ""
    try:
        nc_profile = await nextcloud.current_user_profile(access_token)
        pending_list = await nextcloud.list_pending_shares(access_token)
        nc_pending_count = len(pending_list)
        nc_pending_samples = [
            nextcloud.summarize_share_for_diagnostics(item) for item in pending_list[:5]
        ]
        if nc_pending_count:
            nc_pending_accepted, nc_pending_accept_errors = (
                await nextcloud.accept_all_pending_shares(access_token)
            )
        raw_shares = await nextcloud._list_shares_with_me_raw(access_token)
        nc_shared_with_me_total = len(raw_shares)
        nc_shared_with_me_samples = [
            nextcloud.summarize_share_for_diagnostics(item) for item in raw_shares[:5]
        ]
        nc_share_count = len(await nextcloud.list_shares_with_me_workbooks(access_token))
        nc_dav_count = len(await nextcloud.list_user_dav_workbooks(access_token))
        nc_folder_mount_count = len(
            await nextcloud.list_shared_folder_mounts_for_picker(access_token)
        )
    except Exception as exc:
        nc_error = str(exc)

    visible_docs = await _documents_visible_to_user(user, access_token)

    return {
        "api_primary_email": _user_email(user),
        "jwt_identities": sorted(storage.user_identities(user)),
        "jwt_email": user.get("email"),
        "jwt_preferred_username": user.get("preferred_username"),
        "jwt_sub": user.get("sub"),
        "jwt_name": user.get("name"),
        "nextcloud_user_id": (nc_profile.get("id") or "").strip(),
        "nextcloud_email": (nc_profile.get("email") or "").strip(),
        "nextcloud_displayname": (nc_profile.get("displayname") or "").strip(),
        "nextcloud_shared_workbooks": nc_share_count,
        "nextcloud_shared_with_me_total": nc_shared_with_me_total,
        "nextcloud_dav_workbooks": nc_dav_count,
        "nextcloud_shared_folder_mounts": nc_folder_mount_count,
        "nextcloud_pending_shares": nc_pending_count,
        "nextcloud_pending_shares_accepted": nc_pending_accepted,
        "nextcloud_pending_accept_errors": nc_pending_accept_errors,
        "nextcloud_pending_samples": nc_pending_samples,
        "nextcloud_shared_with_me_samples": nc_shared_with_me_samples,
        "documents_in_api_list": len(visible_docs),
        "documents_in_storage": len(storage.list_documents_for_user(user_ctx)),
        "nextcloud_error": nc_error,
        "share_with_in_nextcloud": (nc_profile.get("id") or nc_profile.get("email") or "").strip(),
        "share_recipient_hint": (
            "User1: Share → user → type full email "
            f"{_user_email(user)} or Nextcloud id "
            f"{(nc_profile.get('id') or '').strip()}. "
            "Public /s/ links do not mount into Files."
        ),
        "hint": (
            "If nextcloud_pending_shares was > 0, shares were waiting for acceptance "
            "(now auto-accepted). If all share counts are 0, User1 did not create a "
            "user share to this account — only a public link or wrong email."
        ),
    }


# ── Root endpoint (requires auth, redirects to login) ─────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Root endpoint: check auth and redirect to oauth login if needed"""
    if not resolve_access_token(request):
        # Not authenticated, redirect to OAuth login
        return RedirectResponse(url="/api/oauth/login", status_code=302)

    # Try to validate token
    try:
        user = await get_current_user(request)
        # Token is valid, show list of documents available for this user
        user_email = _user_email(user)
        access_token = _request_access_token(request)
        docs = await _documents_visible_to_user(user, access_token)

        docs_html = ""
        if docs:
            docs_html = "<h2>Your Documents</h2><ul style='text-align: left; display: inline-block;'>"
            for doc in docs:
                role = storage.get_doc_role(doc, user) or "viewer"
                share_btn = ""
                if role == "owner":
                    share_btn = (
                        f' <button onclick="shareDocument(\'{doc["id"]}\')" style="margin-left: 10px; padding: 4px 8px; font-size: 12px;">Share</button>'
                        f' <button onclick="manageShares(\'{doc["id"]}\')" style="margin-left: 6px; padding: 4px 8px; font-size: 12px;">Manage</button>'
                    )
                docs_html += (
                    f'<li><a href="/api/docs/{doc["id"]}/editor" style="color: #007bff; text-decoration: none;">'
                    f'{doc.get("title", doc["id"])}</a> '
                    f'<span style="color:#777;font-size:12px;">({role})</span>{share_btn}</li>'
                )
            docs_html += "</ul>"
        else:
            diag_html = ""
            try:
                nc_profile = await nextcloud.current_user_profile(access_token)
                nc_id = (nc_profile.get("id") or "?").strip()
                nc_email = (nc_profile.get("email") or "—").strip()
                nc_shares = await nextcloud.list_shares_with_me_workbooks(access_token)
                diag_html = (
                    "<div style='margin:16px 0;padding:12px;background:#fff8e1;border:1px solid #ffe082;"
                    "border-radius:6px;text-align:left;font-size:14px;'>"
                    f"<b>No documents in API list.</b><br>"
                    f"API login email: <code>{user_email}</code><br>"
                    f"Nextcloud account: <code>{nc_id}</code>"
                    f" (email: <code>{nc_email}</code>)<br>"
                    f"Nextcloud <code>shared_with_me</code> spreadsheets: <b>{len(nc_shares)}</b><br>"
                    "User1 must share the file with <b>this</b> Nextcloud id/email "
                    "(not only a public <code>/s/</code> link). "
                    f"<a href='/api/session-info'>Full diagnostics</a>"
                    "</div>"
                )
            except Exception as exc:
                diag_html = (
                    f"<p style='color:#b71c1c;'>Could not query Nextcloud as this user: {exc}</p>"
                    "<p>Re-login at /api/oauth/login after redeploy (onlyoffice-client needs "
                    "nextcloud audience in token).</p>"
                )
            docs_html = (
                diag_html
                + "<p>No documents yet. <a href='#' onclick='document.getElementById(\"docName\").focus(); return false;' "
                "style='color: #007bff;'>Create one now</a></p>"
            )

        user_name = user.get("name") or user.get("preferred_username", "User")

        if KEYCLOAK_ISSUER_EXTERNAL and API_EXTERNAL_URL:
            id_token = ""
            session_id = (request.cookies.get(SESSION_COOKIE) or "").strip()
            if session_id:
                session = load_session(session_id)
                if session:
                    id_token = (session.get("id_token") or "").strip()
            if not id_token:
                id_token = (request.cookies.get("id_token") or "").strip()
            _post_logout = API_EXTERNAL_URL.rstrip("/") + "/signed-out"
            params = {
                "client_id": CLIENT_ID,
                "post_logout_redirect_uri": _post_logout,
            }
            if id_token:
                params["id_token_hint"] = id_token
            logout_href = (
                f"{KEYCLOAK_ISSUER_EXTERNAL.rstrip('/')}/protocol/openid-connect/logout"
                f"?{urlencode(params)}"
            )
        else:
            logout_href = "#"

        # Token is valid, show dashboard
        dashboard_html = """<!DOCTYPE html>
<html>
<head>
    <title>OnlyOffice Spreadsheet</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; background: #f5f5f5; }
        .header { background: #007bff; color: white; padding: 20px; text-align: center; }
        .container { max-width: 800px; margin: 40px auto; background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #333; margin-top: 0; }
        h2 { color: #333; margin-top: 30px; }
        p { color: #666; }
        .button { display: inline-block; padding: 12px 24px; background: #007bff; color: white; text-decoration: none; border-radius: 4px; margin: 10px 0; border: none; cursor: pointer; font-size: 16px; }
        .button:hover { background: #0056b3; }
        ul { list-style: none; padding: 0; }
        li { padding: 10px; margin: 5px 0; background: #f9f9f9; border-radius: 4px; }
        a { color: #007bff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .logout { float: right; font-size: 14px; }
    </style>
</head>
<body>
    <div class="header">
        <h1 style="margin: 0;">OnlyOffice Spreadsheet</h1>
        <p style="margin: 10px 0 0 0;">Welcome, """ + user_name + """!</p>
    </div>
    <div class="container">
        """ + docs_html + """

        <h2>Create New Document</h2>
        <form onsubmit="createDocument(event)">
            <input type="text" id="docName" placeholder="Document name" required style="padding: 8px; width: 100%; max-width: 300px; margin: 10px 0;">
            <button type="submit" class="button">Create</button>
        </form>

        <h2>API Usage</h2>
        <p>You can also use the REST API to manage your documents:</p>
        <ul>
            <li><code>GET /docs</code> - List your documents</li>
            <li><code>POST /workspaces/1/docs</code> - Create a new document</li>
            <li><code>GET /docs/{'{doc_id}'}</code> - Open a document in browser</li>
        </ul>
""" + (
            '        <div class="logout" style="margin-top: 40px; text-align: right;">\n'
            f'            <a href="{logout_href}" onclick="alert(\'You have been logged out\')">Logout</a>\n'
            '        </div>\n'
        ) + """
    </div>

    <script>
        async function createDocument(event) {
            event.preventDefault();
            const docName = document.getElementById('docName').value;

            try {
                const response = await fetch('/api/workspaces/1/docs', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ name: docName })
                });

                if (response.ok) {
                    const docId = await response.json();
                    window.location.href = '/api/docs/' + docId;
                } else {
                    alert('Failed to create document');
                }
            } catch (error) {
                alert('Error: ' + error.message);
            }
        }

        async function shareDocument(docId) {
            const email = prompt('Share with email:');
            if (!email) return;
            const roleInput = prompt('Role (viewer/editor):', 'viewer');
            const role = (roleInput || 'viewer').toLowerCase();
            if (!['viewer', 'editor'].includes(role)) {
                alert('Role must be viewer or editor');
                return;
            }

            try {
                const response = await fetch('/api/docs/' + docId + '/share', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, role })
                });
                if (response.ok) {
                    alert('Access granted: ' + email + ' (' + role + ')');
                    window.location.reload();
                } else {
                    const text = await response.text();
                    alert('Share failed: ' + text);
                }
            } catch (error) {
                alert('Error: ' + error.message);
            }
        }

        async function manageShares(docId) {
            try {
                const response = await fetch('/api/docs/' + docId + '/shares');
                if (!response.ok) {
                    const text = await response.text();
                    alert('Failed to load shares: ' + text);
                    return;
                }
                const data = await response.json();
                const shares = data.shares || [];
                if (!shares.length) {
                    alert('No shared users yet');
                    return;
                }
                const listText = shares.map((s, i) => (i + 1) + '. ' + s.email + ' (' + s.role + ')').join('\\n');
                const toRevoke = prompt('Shared users:\\n' + listText + '\\n\\nType email to revoke access (or leave empty):', '');
                if (!toRevoke) return;

                const revokeResp = await fetch('/api/docs/' + docId + '/share?email=' + encodeURIComponent(toRevoke), {
                    method: 'DELETE'
                });
                if (revokeResp.ok) {
                    alert('Access revoked for ' + toRevoke);
                    window.location.reload();
                } else {
                    const text = await revokeResp.text();
                    alert('Revoke failed: ' + text);
                }
            } catch (error) {
                alert('Error: ' + error.message);
            }
        }
    </script>
</body>
</html>"""
        return HTMLResponse(dashboard_html)
    except HTTPException:
        # Token is invalid, redirect to login
        return RedirectResponse(url="/api/oauth/login", status_code=302)


# ── OAuth2 PKCE helpers ──────────────────────────────────────────────────────
def generate_pkce_pair():
    """Generate code_verifier and code_challenge for PKCE"""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')
    code_sha = hashlib.sha256(code_verifier.encode('utf-8')).digest()
    code_challenge = base64.urlsafe_b64encode(code_sha).decode('utf-8').rstrip('=')
    return code_verifier, code_challenge


# ── OAuth2 Login (Authorization Code Flow with PKCE) ──────────────────────────
@app.get("/oauth/login")
async def oauth_login(request: Request, doc_id: str = "", redirect_to: str = ""):
    """Redirect to Keycloak for OAuth2 login"""
    if not KEYCLOAK_AUTH_BASE or not CLIENT_ID:
        return HTMLResponse("<h1>Keycloak not configured</h1>", status_code=500)
    if redirect_to and not redirect_to.startswith("/api"):
        redirect_to = ""

    code_verifier, code_challenge = generate_pkce_pair()

    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": "openid profile email",
        "redirect_uri": f"{API_EXTERNAL_URL}/oauth/callback",
        "state": secrets.token_urlsafe(32),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    auth_url = f"{KEYCLOAK_AUTH_BASE}/protocol/openid-connect/auth?{urlencode(params)}"

    secure_cookie = _cookie_secure(request)
    response = RedirectResponse(url=auth_url)
    _clear_api_cookies(response)
    response.set_cookie(
        "pkce_verifier",
        code_verifier,
        max_age=600,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        path=API_COOKIE_PATH,
    )
    response.set_cookie(
        "oauth_state",
        params["state"],
        max_age=600,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        path=API_COOKIE_PATH,
    )
    if doc_id:
        response.set_cookie(
            "oauth_doc_id",
            doc_id,
            max_age=600,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            path=API_COOKIE_PATH,
        )
    if redirect_to:
        response.set_cookie(
            "oauth_redirect_to",
            redirect_to,
            max_age=600,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            path=API_COOKIE_PATH,
        )

    return response


# ── OAuth2 Callback ──────────────────────────────────────────────────────────
@app.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    pkce_verifier: str = Cookie(None),
    oauth_state: str = Cookie(None),
    oauth_doc_id: str = Cookie(None),
    oauth_redirect_to: str = Cookie(None),
):
    """Handle OAuth2 callback from Keycloak"""
    if not code or not KEYCLOAK_TOKEN_BASE or not CLIENT_ID or not CLIENT_SECRET:
        return HTMLResponse("<h1>Authentication failed: missing parameters</h1>", status_code=400)

    if not pkce_verifier:
        return HTMLResponse(
            "<h1>Authentication failed: login session expired (PKCE cookie missing).</h1>"
            "<p>Open <a href='/api/oauth/login'>/api/oauth/login</a> and sign in again. "
            "Do not bookmark the callback URL.</p>",
            status_code=400,
        )

    if state != oauth_state:
        return HTMLResponse("<h1>Authentication failed: state mismatch</h1>", status_code=400)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_response = await client.post(
                f"{KEYCLOAK_TOKEN_BASE}/protocol/openid-connect/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": f"{API_EXTERNAL_URL}/oauth/callback",
                    "code_verifier": pkce_verifier,
                },
            )

        if token_response.status_code != 200:
            logger.error(
                "Token exchange failed (%s) via %s: %s",
                token_response.status_code,
                KEYCLOAK_TOKEN_BASE,
                token_response.text,
            )
            return HTMLResponse(
                f"<h1>Token exchange failed ({token_response.status_code})</h1>"
                f"<pre>{token_response.text}</pre>",
                status_code=400,
            )

        token_data = token_response.json()
        access_token = token_data.get("access_token")
        id_token = token_data.get("id_token", "")

        if not access_token:
            return HTMLResponse("<h1>No access token in response</h1>", status_code=400)

        # Redirect to specified location or default to dashboard
        if oauth_redirect_to:
            redirect_url = oauth_redirect_to
        elif oauth_doc_id:
            redirect_url = f"/api/docs/{oauth_doc_id}/editor"
        else:
            redirect_url = "/api/"

        secure_cookie = _cookie_secure(request)
        response = RedirectResponse(url=redirect_url)
        _clear_api_cookies(response)
        # Store JWTs server-side; large Set-Cookie headers can make nginx return 502.
        session_id = save_tokens(access_token, id_token)
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=43200,
            httponly=True,
            secure=secure_cookie,
            samesite="lax",
            path=API_COOKIE_PATH,
        )
        logger.info(
            "OAuth login ok: access_token_len=%s session=%s",
            len(access_token),
            session_id[:8],
        )
        return response

    except httpx.HTTPError as e:
        logger.exception("Token exchange HTTP error via %s", KEYCLOAK_TOKEN_BASE)
        return HTMLResponse(
            f"<h1>Authentication error: cannot reach Keycloak token endpoint</h1>"
            f"<p>{e}</p><p>Check API container network access to {KEYCLOAK_TOKEN_BASE}.</p>",
            status_code=502,
        )
    except Exception as e:
        logger.exception("OAuth callback failed")
        return HTMLResponse(f"<h1>Authentication error: {str(e)}</h1>", status_code=500)


@app.get("/signed-out")
async def signed_out(request: Request):
    """
    Post-logout landing page from Keycloak.
    Clears local cookies and returns user to the dashboard entrypoint.
    """
    delete_session((request.cookies.get(SESSION_COOKIE) or "").strip())
    response = RedirectResponse(url="/api/", status_code=302)
    _clear_api_cookies(response)
    return response


# ── OAuth2 Login Page (simple form fallback) ────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(doc_id: str = ""):
    """Simple HTML page to authenticate and open editor"""
    if not KEYCLOAK_AUTH_BASE:
        return "<h1>Keycloak not configured</h1>"

    return """<!DOCTYPE html>
    <html>
    <head>
        <title>OnlyOffice Login</title>
        <style>
            body { font-family: Arial, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f5f5f5; }
            .container { background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 400px; }
            h1 { color: #333; text-align: center; }
            .form-group { margin: 20px 0; }
            label { display: block; margin-bottom: 5px; font-weight: bold; }
            input { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
            button { width: 100%; padding: 10px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; margin-top: 10px; }
            button:hover { background: #0056b3; }
            .error { color: #dc3545; margin: 10px 0; }
            .loading { display: none; text-align: center; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>OnlyOffice</h1>
            <form id="loginForm" onsubmit="handleLogin(event)">
                <div class="form-group">
                    <label for="email">Email:</label>
                    <input type="email" id="email" name="email" value="ruslan.musagitov@gmail.com" required>
                </div>
                <div class="form-group">
                    <label for="password">Password:</label>
                    <input type="password" id="password" name="password" required autofocus>
                </div>
                <button type="submit">Login</button>
            </form>
            <div class="loading" id="loading">Authenticating...</div>
            <div class="error" id="error"></div>
        </div>

        <script>
            async function handleLogin(event) {
                event.preventDefault();

                document.getElementById('loading').style.display = 'block';
                document.getElementById('error').innerText = '';

                const email = document.getElementById('email').value;
                const password = document.getElementById('password').value;

                try {
                    const response = await fetch('{0}/protocol/openid-connect/token', {
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: new URLSearchParams({
                            grant_type: 'password',
                            client_id: 'onlyoffice-client',
                            username: email,
                            password: password
                        })
                    });

                    if (!response.ok) {
                        const error = await response.json();
                        throw new Error(error.error_description || 'Authentication failed');
                    }

                    const data = await response.json();
                    const token = data.access_token;

                    // Save token to sessionStorage
                    sessionStorage.setItem('access_token', token);

                    // Redirect to editor or ask for doc_id
                    const docId = '{1}' || prompt('Enter Document ID:');
                    if (docId) {
                        window.location.href = '/api/docs/' + docId + '/editor?token=' + token;
                    }
                } catch (error) {
                    document.getElementById('error').innerText = 'Login failed: ' + error.message;
                    document.getElementById('loading').style.display = 'none';
                }
            }
        </script>
    </body>
    </html>""".format(KEYCLOAK_AUTH_BASE, doc_id)


# ── Document list (Grist-compatible) ──────────────────────────────────────────

def _editor_url(doc_id: str) -> str:
    """Public URL for the auto-refreshing browser editor wrapper."""
    doc_id = (doc_id or "").strip()
    if not doc_id:
        return ""
    base = (API_EXTERNAL_URL or "").rstrip("/")
    if not base:
        return f"/docs/{doc_id}/editor"
    return f"{base}/docs/{doc_id}/editor"


def _doc_list_item(meta: dict, user: dict) -> dict:
    item = {"id": meta["id"], "name": meta["title"]}
    item["editor_url"] = _editor_url(meta["id"])
    nc_path = (meta.get("nextcloud_path") or "").strip()
    file_id = (meta.get("nextcloud_file_id") or "").strip()
    if file_id:
        item["browser_url"] = nextcloud.browser_open_url(file_id, nc_path)
    if nc_path:
        item["nextcloud_path"] = nc_path
    item["parent_path"] = nextcloud.parent_path_from_nextcloud_path(nc_path)
    role = storage.get_doc_role(meta, user)
    item["is_owner"] = role == "owner"
    item["can_read"] = storage.can_read(meta, user)
    item["can_write"] = storage.can_write(meta, user)
    if role:
        item["role"] = role
    if role != "owner":
        owner_label = (meta.get("owner_email") or "").strip().lower()
        if mailer.is_deliverable_email(owner_label):
            item["owner_email"] = owner_label
    return item


def _workbook_index(workbooks: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    by_file_id: dict[str, dict] = {}
    by_path: dict[str, dict] = {}
    for workbook in workbooks:
        file_id = (workbook.get("file_id") or "").strip()
        path = storage._normalize_nc_path(workbook.get("path") or "")
        if file_id:
            by_file_id[file_id] = workbook
        if path:
            by_path[path] = workbook
    return by_file_id, by_path


def _match_workbook(meta: dict, by_file_id: dict[str, dict], by_path: dict[str, dict]) -> dict | None:
    file_id = (meta.get("nextcloud_file_id") or "").strip()
    if file_id and file_id in by_file_id:
        return by_file_id[file_id]
    path = storage._normalize_nc_path(meta.get("nextcloud_path") or "")
    if path and path in by_path:
        return by_path[path]
    return None


def _enrich_documents_from_workbooks(docs: list[dict], workbooks: list[dict]) -> list[dict]:
    by_file_id, by_path = _workbook_index(workbooks)
    enriched: list[dict] = []
    for meta in docs:
        updated = dict(meta)
        workbook = _match_workbook(updated, by_file_id, by_path)
        if workbook:
            wb_path = storage._normalize_nc_path(workbook.get("path") or "")
            if wb_path and not (updated.get("nextcloud_path") or "").strip():
                updated["nextcloud_path"] = wb_path
                storage.ensure_nextcloud_path(updated["id"], wb_path)
            owner_id = (workbook.get("owner_id") or "").strip()
            if owner_id and not (updated.get("owner_email") or "").strip():
                updated["owner_email"] = owner_id
                storage.ensure_owner_email(updated["id"], owner_id)
        enriched.append(updated)
    return enriched


def _nextcloud_workbook_keys(workbooks: list[dict]) -> tuple[set[str], set[str]]:
    file_ids: set[str] = set()
    paths: set[str] = set()
    for workbook in workbooks:
        file_id = (workbook.get("file_id") or "").strip()
        if file_id:
            file_ids.add(file_id)
        path = storage._normalize_nc_path(workbook.get("path") or "")
        if path:
            paths.add(path)
    return file_ids, paths


async def _meta_visible_in_nextcloud(
    meta: dict,
    access_token: str,
    file_ids: set[str],
    paths: set[str],
) -> bool:
    file_id = (meta.get("nextcloud_file_id") or "").strip()
    if file_id and file_id in file_ids:
        return True
    path = storage._normalize_nc_path(meta.get("nextcloud_path") or "")
    if path and path in paths:
        return True
    if path:
        try:
            return await nextcloud.file_exists(path, access_token)
        except Exception:
            return False
    return False


async def _prune_stale_document(meta: dict, user: dict, access_token: str) -> None:
    doc_id = meta.get("id")
    if not doc_id:
        return
    role = storage.get_doc_role(meta, user)
    if role == "owner":
        storage.delete_document(doc_id)
        return
    for identity in storage.user_identities(user):
        try:
            storage.revoke_share(doc_id, identity)
        except FileNotFoundError:
            pass


async def _filter_documents_for_nextcloud(
    docs: list[dict],
    user: dict,
    access_token: str,
    workbooks: list[dict],
) -> list[dict]:
    file_ids, paths = _nextcloud_workbook_keys(workbooks)
    visible: list[dict] = []
    for meta in docs:
        if await _meta_visible_in_nextcloud(meta, access_token, file_ids, paths):
            visible.append(meta)
            continue
        await _prune_stale_document(meta, user, access_token)
    return visible


async def _sync_nextcloud_shares_for_user(
    user: dict,
    access_token: str,
    workbooks: list[dict] | None = None,
) -> None:
    """
    Mirror Nextcloud-visible spreadsheets into API metadata for workspace lists.

    Includes user shares (shared_with_me) and xlsx files in the recipient WebDAV tree.
    Public link-only shares (/s/... from email) are NOT listed until User1 also shares
    with User2's account email in Nextcloud (Share → user), or uses POST /api/docs/.../share.
    """
    recipient = _user_email(user)
    recipient_nc_id = (user.get("nextcloud_user_id") or "").strip().lower()
    if not recipient:
        return
    if workbooks is None:
        try:
            workbooks = await nextcloud.list_accessible_workbooks(access_token)
        except Exception as exc:
            logger.warning("Nextcloud workbook sync failed for %s: %s", recipient, exc)
            return
    nc_shares = workbooks
    if not nc_shares:
        logger.info(
            "Nextcloud workbook sync: no spreadsheets for %s (nc_id=%s)",
            recipient,
            recipient_nc_id[:16] if recipient_nc_id else "-",
        )
        return
    try:
        share_mounts = await nextcloud.list_shared_folder_mounts_for_picker(access_token)
    except Exception as exc:
        logger.warning("Nextcloud share mount listing failed during sync: %s", exc)
        share_mounts = []
    mount_by_name = {
        (item.get("name") or "").strip(): item
        for item in share_mounts
        if (item.get("name") or "").strip()
    }
    folder_share_owners: dict[str, str] = {}
    folder_share_roles: dict[str, str] = {}
    try:
        for share in await nextcloud._list_shares_with_me_raw(access_token):
            if str(share.get("item_type") or "").lower() != "folder":
                continue
            file_target = str(share.get("file_target") or "").strip().strip("/")
            label = str(share.get("label") or "").strip().strip("/")
            mount_name = (file_target.split("/")[0] if file_target else "") or (
                label.split("/")[0] if label else ""
            )
            if not mount_name:
                continue
            owner = str(share.get("uid_owner") or "").strip()
            permissions = int(share.get("permissions") or 1)
            folder_role = nextcloud.permissions_to_role(permissions)
            if owner:
                folder_share_owners[mount_name] = owner
            folder_share_roles[mount_name] = folder_role
            if mount_name not in mount_by_name:
                mount_by_name[mount_name] = {
                    "name": mount_name,
                    "mount_path": storage._normalize_nc_path(file_target) if file_target else "",
                    "parent_path": "",
                    "owner_email": owner,
                }
    except Exception as exc:
        logger.warning("Nextcloud folder share listing failed during sync: %s", exc)

    shared_mount_names = set(mount_by_name) | set(folder_share_owners)

    for share in nc_shares:
        meta = storage.find_document_by_nextcloud_file_id(share.get("file_id", ""))
        if not meta:
            meta = storage.find_document_by_nextcloud_path(share.get("path", ""))
        owner_id = (share.get("owner_id") or "").strip()
        wb_path = storage._normalize_nc_path(share.get("path") or "")
        role = nextcloud.workbook_access_role(
            share,
            wb_path,
            folder_share_roles=folder_share_roles,
            shared_mount_names=shared_mount_names,
        )
        top_folder = nextcloud.top_level_folder(wb_path)
        shared_mount = bool(top_folder and top_folder in shared_mount_names)
        user_is_doc_owner = bool(meta and storage.get_doc_role(meta, user) == "owner")

        if not meta:
            if not wb_path:
                continue
            doc_owner = owner_id
            grant_share = bool(owner_id)
            if not doc_owner:
                if shared_mount:
                    mount = mount_by_name.get(top_folder, {})
                    doc_owner = (
                        (mount.get("owner_email") or "").strip()
                        or folder_share_owners.get(top_folder, "")
                        or recipient
                    )
                    owner_identities = {doc_owner.strip().lower()} if doc_owner else set()
                    grant_share = not owner_identities.intersection(
                        storage.user_identities(user)
                    )
                else:
                    doc_owner = recipient
                    grant_share = False
            meta = storage.create_document(
                share.get("title") or "Spreadsheet",
                doc_owner,
                wb_path,
                nextcloud_file_id=share.get("file_id", ""),
            )
        else:
            if owner_id:
                meta = storage.ensure_owner_email(meta["id"], owner_id) or meta
            elif shared_mount and not (meta.get("owner_email") or "").strip():
                mount = mount_by_name.get(top_folder, {})
                inferred_owner = (
                    (mount.get("owner_email") or "").strip()
                    or folder_share_owners.get(top_folder, "")
                )
                if inferred_owner:
                    meta = storage.ensure_owner_email(meta["id"], inferred_owner) or meta
            grant_share = not user_is_doc_owner and (bool(owner_id) or shared_mount)
        if wb_path:
            meta = storage.ensure_nextcloud_path(meta["id"], wb_path) or meta
        if grant_share:
            try:
                storage.share_document(meta["id"], recipient, role)
                if recipient_nc_id and recipient_nc_id != recipient:
                    storage.share_document(meta["id"], recipient_nc_id, role)
            except ValueError:
                pass


async def _user_with_nextcloud_id(user: dict, access_token: str) -> dict:
    """Attach Nextcloud account id so shares keyed by OIDC uid (not email) still match."""
    enriched = dict(user)
    try:
        profile = await nextcloud.current_user_profile(access_token)
        nc_uid = (profile.get("id") or "").strip()
        if nc_uid:
            enriched["nextcloud_user_id"] = nc_uid
    except Exception:
        pass
    return enriched


async def _merge_visible_with_registered_workbooks(
    visible: list[dict],
    workbooks: list[dict],
    user: dict,
) -> list[dict]:
    """Ensure every Nextcloud workbook the user can access has a matching API document."""
    visible_ids = {(meta.get("id") or "").strip() for meta in visible}
    merged = list(visible)
    for workbook in workbooks:
        meta = storage.find_document_by_nextcloud_file_id(workbook.get("file_id", ""))
        if not meta:
            meta = storage.find_document_by_nextcloud_path(workbook.get("path", ""))
        if not meta or not storage.can_read(meta, user):
            continue
        doc_id = (meta.get("id") or "").strip()
        if doc_id and doc_id not in visible_ids:
            merged.append(meta)
            visible_ids.add(doc_id)
    return merged


async def _documents_visible_to_user(user: dict, access_token: str) -> list[dict]:
    user_ctx = await _user_with_nextcloud_id(user, access_token)
    try:
        pending_accepted, pending_errors = await nextcloud.accept_all_pending_shares(access_token)
        if pending_accepted:
            logger.info("Auto-accepted %s pending Nextcloud share(s)", pending_accepted)
        if pending_errors:
            logger.warning(
                "Pending share acceptance errors: %s",
                "; ".join(pending_errors),
            )
    except Exception as exc:
        logger.warning("Pending share acceptance failed: %s", exc)
    try:
        workbooks = await nextcloud.list_accessible_workbooks(access_token)
    except Exception as exc:
        logger.warning("Nextcloud workbook listing failed: %s", exc)
        workbooks = []
    await _sync_nextcloud_shares_for_user(user_ctx, access_token, workbooks)
    docs = storage.list_documents_for_user(user_ctx)
    visible = await _filter_documents_for_nextcloud(docs, user_ctx, access_token, workbooks)
    visible = await _merge_visible_with_registered_workbooks(visible, workbooks, user_ctx)
    return _enrich_documents_from_workbooks(visible, workbooks)


def _folder_list_item(folder: dict) -> dict:
    item = {
        "name": folder.get("name") or "",
        "parent_path": folder.get("parent_path") or "",
        "is_shared": True,
    }
    owner = (folder.get("owner_email") or "").strip()
    if owner:
        item["owner_email"] = owner
    return item


@app.get("/orgs/{org_id}/workspaces")
async def list_workspaces(
    org_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    access_token = _request_access_token(request)
    docs = await _documents_visible_to_user(user, access_token)
    shared_folders = await nextcloud.list_shared_folder_mounts_for_picker(access_token)
    return [
        {
            "id": 1,
            "name": "Default",
            "docs": [_doc_list_item(d, user) for d in docs],
            "folders": [_folder_list_item(f) for f in shared_folders],
        }
    ]


# ── Document creation (Grist-compatible) ─────────────────────────────────────

@app.post("/workspaces/{workspace_id}/docs")
async def create_doc(
    workspace_id: int,
    req: CreateDocRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    owner_email = _user_email(user)
    access_token = _request_access_token(request)
    nextcloud_path = await nextcloud.reserve_document_path(req.name, access_token)
    await nextcloud.create_empty_workbook(nextcloud_path, access_token)
    try:
        file_id = await nextcloud.resolve_file_id(nextcloud_path, access_token)
    except Exception:
        file_id = ""
    doc = storage.create_document(
        nextcloud.title_from_relative_path(nextcloud_path),
        owner_email,
        nextcloud_path,
        nextcloud_file_id=file_id,
    )
    return doc["id"]


async def _grant_document_access(
    doc_id: str,
    recipient: str,
    role: str,
    access_token: str,
    nextcloud_path: str,
) -> dict:
    """Register API metadata and attempt a Nextcloud user share."""
    recipient = (recipient or "").strip()
    if not recipient:
        raise ValueError("email is required")

    updated = storage.share_document(doc_id, recipient, role)
    nc_user_id = ""
    if "@" in recipient:
        try:
            nc_user_id = await nextcloud.resolve_user_id_for_share(recipient, access_token)
        except ValueError:
            nc_user_id = ""
        if nc_user_id and nc_user_id != recipient:
            try:
                updated = storage.share_document(doc_id, nc_user_id, role)
            except ValueError:
                pass

    nc_share_error = ""
    try:
        await nextcloud.create_user_share(
            nextcloud_path,
            nc_user_id or recipient,
            role=role,
            access_token=access_token,
        )
    except Exception as exc:
        nc_share_error = str(exc)
        logger.warning(
            "Nextcloud share failed doc=%s recipient=%s: %s",
            doc_id,
            recipient,
            exc,
        )

    return {
        "meta": updated,
        "nextcloud_user_id": nc_user_id or None,
        "nextcloud_share_error": nc_share_error or None,
    }


@app.post("/docs/{doc_id}/share")
async def share_doc(
    doc_id: str,
    req: ShareDocRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    meta = storage.get_document_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")

    access_token = _request_access_token(request)
    user_ctx = await _user_with_nextcloud_id(user, access_token)
    role = storage.get_doc_role(meta, user_ctx)
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only owner can share document")

    try:
        nextcloud_path = _doc_nextcloud_path(meta)
        result = await _grant_document_access(
            doc_id, req.email, req.role, access_token, nextcloud_path
        )
        updated = result["meta"]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "doc_id": doc_id,
        "shared_with": updated.get("shared_with", {}),
        "nextcloud_user_id": result.get("nextcloud_user_id"),
        "nextcloud_share_error": result.get("nextcloud_share_error"),
    }


@app.get("/docs/{doc_id}/shares")
async def get_doc_shares(
    doc_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    meta = storage.get_document_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")
    access_token = _request_access_token(request)
    user_ctx = await _user_with_nextcloud_id(user, access_token)
    role = storage.get_doc_role(meta, user_ctx)
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only owner can view shares")
    return {"doc_id": doc_id, "shares": storage.list_shares(doc_id)}


@app.delete("/docs/{doc_id}/share")
async def revoke_doc_share(
    doc_id: str,
    email: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    meta = storage.get_document_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")
    access_token = _request_access_token(request)
    user_ctx = await _user_with_nextcloud_id(user, access_token)
    role = storage.get_doc_role(meta, user_ctx)
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only owner can revoke access")
    nextcloud_path = _doc_nextcloud_path(meta)
    targets = {email.strip()}
    if "@" in email:
        try:
            nc_id = await nextcloud.resolve_user_id_for_share(email, access_token)
            if nc_id:
                targets.add(nc_id)
        except ValueError:
            pass
    for target in targets:
        if not target:
            continue
        try:
            await nextcloud.revoke_user_share(nextcloud_path, target, access_token)
        except Exception as exc:
            logger.warning("Nextcloud revoke failed doc=%s target=%s: %s", doc_id, target, exc)
        try:
            updated = storage.revoke_share(doc_id, target)
        except FileNotFoundError:
            updated = meta
    return {"doc_id": doc_id, "shared_with": updated.get("shared_with", {})}


@app.get("/docs/{doc_id}/meta")
async def get_doc_meta(
    doc_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    meta = storage.get_document_meta(doc_id)
    access_token = _request_access_token(request)
    user_ctx = await _require_doc_access_with_nextcloud_id(
        meta, user, access_token, write=False
    )
    return _doc_list_item(meta, user_ctx)


@app.post("/docs/{doc_id}/request-access")
async def request_doc_access(
    doc_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """
    Accept pending Nextcloud shares. If edit access is still missing, create an access
    request and email the document owner a link to grant or deny (variant A).
    """
    meta = storage.get_document_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")

    access_token = _request_access_token(request)
    user_ctx = await _user_with_nextcloud_id(user, access_token)
    requester_email = _user_email(user_ctx)

    pending_accepted = 0
    pending_errors: list[str] = []
    try:
        pending_accepted, pending_errors = await nextcloud.accept_all_pending_shares(
            access_token
        )
    except Exception as exc:
        logger.warning("Pending share acceptance failed during request-access: %s", exc)

    can_read = storage.can_read(meta, user_ctx)
    can_write = storage.can_write(meta, user_ctx)
    owner_label = (meta.get("owner_email") or "").strip().lower() or None
    owner_notification_email = None
    if owner_label:
        owner_notification_email = await nextcloud.resolve_notification_email(
            owner_label,
            access_token,
        )
        if (
            owner_notification_email
            and owner_notification_email != owner_label
            and not mailer.is_deliverable_email(owner_label)
        ):
            storage.ensure_owner_email(doc_id, owner_notification_email)
    role = storage.get_doc_role(meta, user_ctx)
    doc_title = (meta.get("title") or meta.get("name") or doc_id).strip()

    if can_write:
        status = "granted"
        if pending_accepted:
            status = "pending_accepted"
        return {
            "status": status,
            "can_read": can_read,
            "can_write": can_write,
            "role": role,
            "owner_email": owner_notification_email,
            "requester_email": requester_email,
            "pending_shares_accepted": pending_accepted,
            "pending_accept_errors": pending_errors,
            "email_sent": False,
            "review_url": None,
        }

    access_record = None
    email_sent = False
    email_error = None
    review_url = None

    if not owner_notification_email:
        status = "denied"
        email_error = mailer.user_facing_delivery_error()
    elif requester_email == owner_notification_email:
        status = "denied"
        email_error = "You already own this document"
    else:
        try:
            access_record = access_requests.create_or_refresh_request(
                doc_id=doc_id,
                doc_title=doc_title,
                requester_email=requester_email,
                owner_email=owner_notification_email,
            )
            review_url = _access_request_review_url(access_record["token"])
            if mailer.smtp_configured():
                try:
                    await asyncio.to_thread(
                        mailer.send_access_request_email,
                        owner_email=owner_notification_email,
                        requester_email=requester_email,
                        doc_title=doc_title,
                        review_url=review_url,
                    )
                    email_sent = True
                    status = "request_sent"
                except Exception as exc:
                    logger.warning("Access request email failed: %s", exc)
                    status = "request_saved"
                    email_error = (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else mailer.user_facing_delivery_error()
                    )
            else:
                status = "request_saved"
                email_error = mailer.user_facing_delivery_error()
        except ValueError as exc:
            status = "denied"
            email_error = str(exc)

    if pending_accepted and not can_write:
        can_read = storage.can_read(meta, user_ctx)
        can_write = storage.can_write(meta, user_ctx)
        role = storage.get_doc_role(meta, user_ctx)
        if can_write:
            status = "pending_accepted"

    return {
        "status": status,
        "can_read": can_read,
        "can_write": can_write,
        "role": role,
        "owner_email": owner_notification_email,
        "requester_email": requester_email,
        "pending_shares_accepted": pending_accepted,
        "pending_accept_errors": pending_errors,
        "email_sent": email_sent,
        "email_error": email_error,
        "review_url": review_url,
        "access_request_token": (access_record or {}).get("token"),
    }


@app.get("/access-requests/{token}", response_class=HTMLResponse)
async def access_request_review_page(token: str, request: Request):
    record = access_requests.get_request(token)
    if not record:
        return HTMLResponse(
            access_request_ui.result_page(
                title="Access request not found",
                message="This link is invalid or has expired.",
                tone="err",
            ),
            status_code=404,
        )

    user = await _optional_current_user(request)
    logged_in_email = _user_email(user) if user else None
    login_url = (
        f"/api/oauth/login?redirect_to=/api/access-requests/{quote(token, safe='')}"
    )
    error = ""
    mode = "login"
    if user:
        if not logged_in_email:
            error = "Could not determine your account email."
            mode = "readonly"
        else:
            meta = storage.get_document_meta(record.get("doc_id") or "")
            if not meta:
                error = "Document no longer exists."
                mode = "readonly"
            elif not _is_document_owner(meta, user):
                error = (
                    f"Only the document owner ({record.get('owner_email') or 'unknown'}) "
                    "can respond to this request."
                )
                mode = "readonly"
            else:
                mode = "respond"

    html = access_request_ui.review_page(
        record=record,
        mode=mode,
        logged_in_email=logged_in_email,
        login_url=login_url,
        grant_action=f"/api/access-requests/{quote(token, safe='')}/grant",
        deny_action=f"/api/access-requests/{quote(token, safe='')}/deny",
        error=error,
    )
    return HTMLResponse(html)


async def _resolve_access_request_as_owner(
    token: str,
    request: Request,
    user: dict,
) -> tuple[dict, dict, str]:
    record = access_requests.get_request(token)
    if not record:
        raise HTTPException(status_code=404, detail="Access request not found")
    if record.get("status") != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Request already {record.get('status')}",
        )

    meta = storage.get_document_meta(record.get("doc_id") or "")
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")

    user_ctx = await _user_with_nextcloud_id(
        user,
        _request_access_token(request),
    )
    if not _is_document_owner(meta, user_ctx):
        raise HTTPException(status_code=403, detail="Only the document owner can respond")

    return record, meta, _request_access_token(request)


@app.post("/access-requests/{token}/grant", response_class=HTMLResponse)
async def grant_access_request(
    token: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    try:
        record, meta, access_token = await _resolve_access_request_as_owner(
            token, request, user
        )
        await _grant_document_access(
            record["doc_id"],
            record["requester_email"],
            record.get("requested_role") or "editor",
            access_token,
            _doc_nextcloud_path(meta),
        )
        access_requests.update_request_status(
            token,
            status="granted",
            resolved_by=_user_email(user),
        )
        requester = record.get("requester_email") or "the user"
        doc_title = record.get("doc_title") or record.get("doc_id") or "spreadsheet"
        return HTMLResponse(
            access_request_ui.result_page(
                title="Access granted",
                message=(
                    f"Edit access to {doc_title} was granted to {requester}."
                ),
                tone="ok",
            )
        )
    except HTTPException as exc:
        return HTMLResponse(
            access_request_ui.result_page(
                title="Could not grant access",
                message=exc.detail,
                tone="err",
            ),
            status_code=exc.status_code,
        )
    except Exception as exc:
        logger.exception("Grant access request failed")
        return HTMLResponse(
            access_request_ui.result_page(
                title="Could not grant access",
                message=str(exc),
                tone="err",
            ),
            status_code=500,
        )


@app.post("/access-requests/{token}/deny", response_class=HTMLResponse)
async def deny_access_request(
    token: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    try:
        record, _, _ = await _resolve_access_request_as_owner(token, request, user)
        access_requests.update_request_status(
            token,
            status="denied",
            resolved_by=_user_email(user),
        )
        requester = record.get("requester_email") or "the user"
        doc_title = record.get("doc_title") or record.get("doc_id") or "spreadsheet"
        return HTMLResponse(
            access_request_ui.result_page(
                title="Access denied",
                message=(
                    f"The request from {requester} for {doc_title} was denied."
                ),
                tone="warn",
            )
        )
    except HTTPException as exc:
        return HTMLResponse(
            access_request_ui.result_page(
                title="Could not deny request",
                message=exc.detail,
                tone="err",
            ),
            status_code=exc.status_code,
        )


@app.get("/docs/{doc_id}/tables")
async def list_tables(
    doc_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    meta = storage.get_document_meta(doc_id)
    access_token = _request_access_token(request)
    await _require_doc_access_with_nextcloud_id(meta, user, access_token, write=False)
    table_names = await _with_doc_workbook(
        doc_id,
        access_token,
        lambda local_path: spreadsheet.list_sheets(local_path),
        upload=False,
    )
    return {"tables": [{"id": name, "columns": []} for name in table_names]}


@app.post("/docs/{doc_id}/tables")
async def create_tables(
    doc_id: str,
    req: CreateTablesRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    meta = storage.get_document_meta(doc_id)
    access_token = _request_access_token(request)
    await _require_doc_access_with_nextcloud_id(meta, user, access_token, write=True)
    def init_tables(local_path: Path) -> None:
        for table in req.tables:
            spreadsheet.init_sheet(local_path, table.id, [c.id for c in table.columns])
    await _with_doc_workbook(doc_id, access_token, init_tables, upload=True, bump_revision=True)

    return {}


# ── Row operations (Grist-compatible) ────────────────────────────────────────

@app.post("/docs/{doc_id}/tables/{table_id}/records")
async def add_records(
    doc_id: str,
    table_id: str,
    req: AddRecordsRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    meta = storage.get_document_meta(doc_id)
    access_token = _request_access_token(request)
    await _require_doc_access_with_nextcloud_id(meta, user, access_token, write=True)
    def append(local_path: Path) -> None:
        spreadsheet.append_rows(
            local_path,
            table_id,
            [r.fields for r in req.records],
        )
    await _with_doc_workbook(doc_id, access_token, append, upload=True, bump_revision=True)
    return {}


@app.get("/docs/{doc_id}/tables/{table_id}/records")
async def get_records(
    doc_id: str,
    table_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    meta = storage.get_document_meta(doc_id)
    access_token = _request_access_token(request)
    await _require_doc_access_with_nextcloud_id(meta, user, access_token, write=False)
    rows = await _with_doc_workbook(
        doc_id,
        access_token,
        lambda local_path: spreadsheet.get_rows(local_path, table_id),
        upload=False,
    )
    return {"records": [{"fields": r} for r in rows]}


# ── OnlyOffice browser editor ─────────────────────────────────────────────────

@app.get("/docs/{doc_id}", response_class=HTMLResponse)
async def open_document(doc_id: str, request: Request):
    """Open document in browser - with auth redirect if needed"""
    token = resolve_access_token(request)
    if not token:
        # Not authenticated, redirect to OAuth login
        return RedirectResponse(url=f"/api/oauth/login?redirect_to=/api/docs/{doc_id}", status_code=302)

    # Token exists, try to validate it
    try:
        jwks = await _fetch_jwks()
        payload = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            issuer=KEYCLOAK_ISSUER_EXTERNAL,
            options={"verify_aud": False},
        )
        meta = storage.get_document_meta(doc_id)
        user_ctx = await _user_with_nextcloud_id(payload, token)
        if not storage.can_read(meta, user_ctx):
            raise HTTPException(status_code=403, detail="Access denied")
        # Token is valid, show the editor
        return RedirectResponse(url=f"/api/docs/{doc_id}/editor", status_code=302)
    except HTTPException:
        raise
    except Exception:
        # Token is invalid, redirect to login
        return RedirectResponse(url=f"/api/oauth/login?redirect_to=/api/docs/{doc_id}", status_code=302)


@app.get("/docs/{doc_id}/revision")
async def get_doc_revision(
    doc_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Revision counter bumped when mobile/API writes rows; used by the browser editor wrapper."""
    meta = storage.get_document_meta(doc_id)
    access_token = _request_access_token(request)
    await _require_doc_access_with_nextcloud_id(meta, user, access_token, write=False)
    return {"revision": storage.get_content_revision(meta)}


@app.get("/docs/{doc_id}/editor-config")
async def get_editor_config(
    doc_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    since: int = 0,
    editor_session_id: str = "",
):
    """Fresh signed OnlyOffice config when content_revision increases (for refreshFile)."""
    meta = storage.get_document_meta(doc_id)
    access_token = _request_access_token(request)
    user_ctx = await _require_doc_access_with_nextcloud_id(
        meta, user, access_token, write=False
    )
    revision = storage.get_content_revision(meta)
    if revision <= since:
        return {"revision": revision}

    session = editor_session.load_editor_session(editor_session_id)
    if not session or session.get("doc_id") != doc_id:
        editor_session_id = editor_session.save_editor_session(doc_id, access_token)

    config = _build_editor_config(meta, user_ctx, doc_id, editor_session_id, revision)
    return {"revision": revision, "config": config}


@app.get("/docs/{doc_id}/editor", response_class=HTMLResponse)
async def get_editor(doc_id: str, request: Request, user: dict = Depends(get_current_user)):
    """
    Open OnlyOffice in the browser. Embedded mode polls content_revision and calls
    refreshFile() when mobile/API writes rows (no full page reload).
    """
    meta = storage.get_document_meta(doc_id)
    access_token = _request_access_token(request)
    user_ctx = await _require_doc_access_with_nextcloud_id(
        meta, user, access_token, write=False
    )
    poll_ms = int(os.getenv("EDITOR_REFRESH_POLL_MS", "3000"))
    revision = storage.get_content_revision(meta)

    if EDITOR_MODE == "iframe":
        try:
            url = await _browser_open_url(meta, access_token)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not open document in Nextcloud: {exc}")
        return HTMLResponse(
            onlyoffice.render_editor_watch_page(
                doc_id=doc_id,
                frame_url=url,
                revision=revision,
                poll_ms=poll_ms,
            )
        )

    editor_session_id = editor_session.save_editor_session(doc_id, access_token)
    config = _build_editor_config(meta, user_ctx, doc_id, editor_session_id, revision)
    return HTMLResponse(
        onlyoffice.render_editor_embed_page(
            doc_id=doc_id,
            editor_session=editor_session_id,
            config=config,
            revision=revision,
            poll_ms=poll_ms,
        )
    )


@app.get("/docs/{doc_id}/file.xlsx")
async def get_file(
    doc_id: str,
    request: Request,
    access_token: str = "",
    editor_session: str = "",
):
    """Called by OnlyOffice Document Server to fetch the file — no user auth."""
    meta = storage.get_document_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")
    nextcloud_path = _doc_nextcloud_path(meta)
    if not nextcloud_path:
        raise HTTPException(status_code=404, detail="Document storage path is missing")
    token = _nextcloud_token_for_editor(
        request,
        doc_id,
        editor_session_id=editor_session,
        access_token=access_token,
    )
    file_id = (meta.get("nextcloud_file_id") or "").strip()
    if not file_id and nextcloud_path:
        try:
            file_id = await nextcloud.resolve_file_id(nextcloud_path, token)
            if file_id:
                meta["nextcloud_file_id"] = file_id
                storage.save_document_meta(doc_id, meta)
        except Exception as exc:
            logger.warning("get_file could not resolve file_id doc_id=%s: %s", doc_id, exc)
    try:
        content = await nextcloud.download_bytes(nextcloud_path, token, file_id=file_id)
    except Exception as exc:
        logger.warning(
            "get_file failed doc_id=%s path=%s session=%s: %s",
            doc_id,
            nextcloud_path,
            bool(editor_session),
            exc,
        )
        raise HTTPException(status_code=502, detail="Could not download spreadsheet from storage") from exc
    filename = nextcloud.file_name_from_relative_path(nextcloud_path)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": nextcloud.content_disposition_inline(filename)},
    )


@app.post("/docs/{doc_id}/callback")
async def onlyoffice_callback(
    doc_id: str,
    request: Request,
    body: dict,
    access_token: str = "",
    editor_session: str = "",
):
    """
    OnlyOffice save callback.
    status=2 → document ready; download from body['url'] and persist.
    """
    status = body.get("status")
    if status == 2:
        download_url = body.get("url")
        if download_url:
            async with httpx.AsyncClient() as client:
                resp = await client.get(download_url, timeout=30)
                resp.raise_for_status()
            meta = storage.get_document_meta(doc_id)
            nextcloud_path = _doc_nextcloud_path(meta)
            if not nextcloud_path:
                raise HTTPException(status_code=404, detail="Document storage path is missing")
            token = _nextcloud_token_for_editor(
                request,
                doc_id,
                editor_session_id=editor_session,
                access_token=access_token,
            )
            await nextcloud.upload_bytes(nextcloud_path, resp.content, token)
    return {"error": 0}
