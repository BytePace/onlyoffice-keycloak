import os

import httpx
from fastapi import HTTPException, Request
from jose import JWTError, jwt

from .session_store import SESSION_COOKIE, load_session

# Internal issuer used for JWKS fetch (container-to-container)
KEYCLOAK_ISSUER = os.getenv("KEYCLOAK_ISSUER", "")
# External issuer embedded in tokens (public URL)
KEYCLOAK_ISSUER_EXTERNAL = os.getenv("KEYCLOAK_ISSUER_EXTERNAL", KEYCLOAK_ISSUER)

_jwks_cache: dict | None = None


async def _fetch_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache is None:
        url = f"{KEYCLOAK_ISSUER}/protocol/openid-connect/certs"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10)
            resp.raise_for_status()
            _jwks_cache = resp.json()
    return _jwks_cache


def invalidate_jwks_cache() -> None:
    global _jwks_cache
    _jwks_cache = None


def resolve_access_token(request: Request) -> str | None:
    """Bearer header, legacy access_token cookie, or server-side api_session."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token:
            return token
    cookie_token = (request.cookies.get("access_token") or "").strip()
    if cookie_token:
        return cookie_token
    session_id = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if session_id:
        session = load_session(session_id)
        if session:
            return (session.get("access_token") or "").strip()
    return None


async def get_current_user(request: Request) -> dict:
    """Get current user from Bearer token or cookie"""
    token = resolve_access_token(request)

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        jwks = await _fetch_jwks()
        payload = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            issuer=KEYCLOAK_ISSUER_EXTERNAL,
            options={"verify_aud": False},
        )
        return payload
    except JWTError as exc:
        invalidate_jwks_cache()  # Force JWKS refresh on next request
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Authentication failed: {exc}")
