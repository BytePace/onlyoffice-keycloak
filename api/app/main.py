import os
import secrets
import base64
import hashlib
from urllib.parse import urlencode, parse_qs, urlparse

import httpx
from fastapi import Depends, HTTPException, Request, Cookie
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi import FastAPI

from .auth import get_current_user
from .models import AddRecordsRequest, CreateDocRequest, CreateTablesRequest
from . import onlyoffice, spreadsheet, storage

app = FastAPI(title="OnlyOffice Spreadsheet API", version="1.0.0")

API_EXTERNAL_URL = os.getenv("API_EXTERNAL_URL", "")
KEYCLOAK_ISSUER = os.getenv("KEYCLOAK_ISSUER_EXTERNAL", "")
CLIENT_ID = "onlyoffice-client"
CLIENT_SECRET = os.getenv("OO_CLIENT_SECRET", "")


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

    # Return normal error response
    return HTMLResponse(
        f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>",
        status_code=exc.status_code
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── OAuth2 PKCE helpers ──────────────────────────────────────────────────────
def generate_pkce_pair():
    """Generate code_verifier and code_challenge for PKCE"""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8').rstrip('=')
    code_sha = hashlib.sha256(code_verifier.encode('utf-8')).digest()
    code_challenge = base64.urlsafe_b64encode(code_sha).decode('utf-8').rstrip('=')
    return code_verifier, code_challenge


# ── OAuth2 Login (Authorization Code Flow with PKCE) ──────────────────────────
@app.get("/oauth/login")
async def oauth_login(doc_id: str = "", redirect_to: str = ""):
    """Redirect to Keycloak for OAuth2 login"""
    if not KEYCLOAK_ISSUER or not CLIENT_ID:
        return HTMLResponse("<h1>Keycloak not configured</h1>", status_code=500)

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

    # Store code_verifier in cookie (valid for 10 minutes)
    auth_url = f"{KEYCLOAK_ISSUER}/protocol/openid-connect/auth?{urlencode(params)}"

    response = RedirectResponse(url=auth_url)
    response.set_cookie(
        "pkce_verifier",
        code_verifier,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="lax"
    )
    response.set_cookie(
        "oauth_state",
        params["state"],
        max_age=600,
        httponly=True,
        secure=True,
        samesite="lax"
    )
    if doc_id:
        response.set_cookie(
            "oauth_doc_id",
            doc_id,
            max_age=600,
            httponly=True,
            secure=True,
            samesite="lax"
        )

    return response


# ── OAuth2 Callback ──────────────────────────────────────────────────────────
@app.get("/oauth/callback")
async def oauth_callback(
    code: str = "",
    state: str = "",
    pkce_verifier: str = Cookie(None),
    oauth_state: str = Cookie(None),
    oauth_doc_id: str = Cookie(None),
):
    """Handle OAuth2 callback from Keycloak"""
    if not code or not KEYCLOAK_ISSUER or not CLIENT_ID or not CLIENT_SECRET:
        return HTMLResponse("<h1>Authentication failed: missing parameters</h1>", status_code=400)

    if state != oauth_state:
        return HTMLResponse("<h1>Authentication failed: state mismatch</h1>", status_code=400)

    try:
        # Exchange code for token
        token_response = await httpx.AsyncClient().post(
            f"{KEYCLOAK_ISSUER}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{API_EXTERNAL_URL}/oauth/callback",
                "code_verifier": pkce_verifier,
            }
        )

        if token_response.status_code != 200:
            return HTMLResponse(f"<h1>Token exchange failed: {token_response.text}</h1>", status_code=400)

        token_data = token_response.json()
        access_token = token_data.get("access_token")

        if not access_token:
            return HTMLResponse("<h1>No access token in response</h1>", status_code=400)

        # Redirect to editor or dashboard
        doc_id = oauth_doc_id or ""
        redirect_url = f"/api/docs/{doc_id}/editor" if doc_id else "/api/"

        response = RedirectResponse(url=redirect_url)
        response.set_cookie(
            "access_token",
            access_token,
            max_age=3600,
            httponly=True,
            secure=True,
            samesite="lax"
        )
        return response

    except Exception as e:
        return HTMLResponse(f"<h1>Authentication error: {str(e)}</h1>", status_code=500)


# ── OAuth2 Login Page (simple form fallback) ────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(doc_id: str = ""):
    """Simple HTML page to authenticate and open editor"""
    if not KEYCLOAK_ISSUER:
        return "<h1>Keycloak not configured</h1>"

    return f"""<!DOCTYPE html>
    <html>
    <head>
        <title>OnlyOffice Login</title>
        <style>
            body {{ font-family: Arial, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #f5f5f5; }}
            .container {{ background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 400px; }}
            h1 {{ color: #333; text-align: center; }}
            .form-group {{ margin: 20px 0; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
            input {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
            button {{ width: 100%; padding: 10px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; margin-top: 10px; }}
            button:hover {{ background: #0056b3; }}
            .error {{ color: #dc3545; margin: 10px 0; }}
            .loading {{ display: none; text-align: center; }}
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
            async function handleLogin(event) {{
                event.preventDefault();

                document.getElementById('loading').style.display = 'block';
                document.getElementById('error').innerText = '';

                const email = document.getElementById('email').value;
                const password = document.getElementById('password').value;

                try {{
                    const response = await fetch('{KEYCLOAK_ISSUER}/protocol/openid-connect/token', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: new URLSearchParams({{
                            grant_type: 'password',
                            client_id: 'onlyoffice-client',
                            username: email,
                            password: password
                        }})
                    }});

                    if (!response.ok) {{
                        const error = await response.json();
                        throw new Error(error.error_description || 'Authentication failed');
                    }}

                    const data = await response.json();
                    const token = data.access_token;

                    // Save token to sessionStorage
                    sessionStorage.setItem('access_token', token);

                    // Redirect to editor or ask for doc_id
                    const docId = '{doc_id}' || prompt('Enter Document ID:');
                    if (docId) {{
                        window.location.href = '/api/docs/' + docId + '/editor?token=' + token;
                    }}
                }} catch (error) {{
                    document.getElementById('error').innerText = 'Login failed: ' + error.message;
                    document.getElementById('loading').style.display = 'none';
                }}
            }}
        </script>
    </body>
    </html>"""


# ── Document list (Grist-compatible) ──────────────────────────────────────────

@app.get("/orgs/{org_id}/workspaces")
async def list_workspaces(org_id: str, user: dict = Depends(get_current_user)):
    docs = storage.list_documents()
    return [
        {
            "id": 1,
            "name": "Default",
            "docs": [{"id": d["id"], "name": d["title"]} for d in docs],
        }
    ]


# ── Document creation (Grist-compatible) ─────────────────────────────────────

@app.post("/workspaces/{workspace_id}/docs")
async def create_doc(
    workspace_id: int,
    req: CreateDocRequest,
    user: dict = Depends(get_current_user),
):
    doc = storage.create_document(req.name)
    return doc["id"]


@app.post("/docs/{doc_id}/tables")
async def create_tables(
    doc_id: str,
    req: CreateTablesRequest,
    user: dict = Depends(get_current_user),
):
    meta = storage.get_document_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")

    xlsx_path = storage.get_xlsx_path(doc_id)
    for table in req.tables:
        spreadsheet.init_sheet(xlsx_path, table.id, [c.id for c in table.columns])

    return {}


# ── Row operations (Grist-compatible) ────────────────────────────────────────

@app.post("/docs/{doc_id}/tables/{table_id}/records")
async def add_records(
    doc_id: str,
    table_id: str,
    req: AddRecordsRequest,
    user: dict = Depends(get_current_user),
):
    meta = storage.get_document_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")

    spreadsheet.append_rows(
        storage.get_xlsx_path(doc_id),
        table_id,
        [r.fields for r in req.records],
    )
    return {}


@app.get("/docs/{doc_id}/tables/{table_id}/records")
async def get_records(
    doc_id: str,
    table_id: str,
    user: dict = Depends(get_current_user),
):
    meta = storage.get_document_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")

    rows = spreadsheet.get_rows(storage.get_xlsx_path(doc_id), table_id)
    return {"records": [{"fields": r} for r in rows]}


# ── OnlyOffice browser editor ─────────────────────────────────────────────────

@app.get("/docs/{doc_id}/editor", response_class=HTMLResponse)
async def get_editor(doc_id: str, user: dict = Depends(get_current_user)):
    meta = storage.get_document_meta(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Document not found")

    user_email = user.get("email") or user.get("sub", "user")
    config = onlyoffice.build_editor_config(
        doc_id=doc_id,
        title=meta["title"],
        user_email=user_email,
        file_url=f"{API_EXTERNAL_URL}/api/docs/{doc_id}/file.xlsx",
        callback_url=f"{API_EXTERNAL_URL}/api/docs/{doc_id}/callback",
    )
    return HTMLResponse(content=onlyoffice.render_editor_html(config))


@app.get("/docs/{doc_id}/file.xlsx")
async def get_file(doc_id: str):
    """Called by OnlyOffice Document Server to fetch the file — no user auth."""
    xlsx_path = storage.get_xlsx_path(doc_id)
    if not xlsx_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        xlsx_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{doc_id}.xlsx",
    )


@app.post("/docs/{doc_id}/callback")
async def onlyoffice_callback(doc_id: str, body: dict):
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
            xlsx_path = storage.get_xlsx_path(doc_id)
            xlsx_path.write_bytes(resp.content)
    return {"error": 0}
