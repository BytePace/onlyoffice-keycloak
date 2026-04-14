import os

import httpx
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

# Internal issuer used for JWKS fetch (container-to-container)
KEYCLOAK_ISSUER = os.getenv("KEYCLOAK_ISSUER", "")
# External issuer embedded in tokens (public URL)
KEYCLOAK_ISSUER_EXTERNAL = os.getenv("KEYCLOAK_ISSUER_EXTERNAL", KEYCLOAK_ISSUER)

bearer = HTTPBearer()

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


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
) -> dict:
    token = credentials.credentials
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
