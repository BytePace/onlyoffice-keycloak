"""Server-side OAuth sessions — avoids huge JWT Set-Cookie headers (nginx 502)."""

import json
import os
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SESSIONS_DIR = DATA_DIR / "sessions"
SESSION_TTL = timedelta(hours=12)
SESSION_COOKIE = "api_session"


def _session_path(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")
    return SESSIONS_DIR / f"{safe}.json"


def save_tokens(access_token: str, id_token: str = "") -> str:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_id = secrets.token_urlsafe(24)
    payload = {
        "access_token": (access_token or "").strip(),
        "id_token": (id_token or "").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(_session_path(session_id), "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    return session_id


def load_session(session_id: str) -> dict | None:
    if not (session_id or "").strip():
        return None
    path = _session_path(session_id)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    created_raw = (data.get("created_at") or "").strip()
    if created_raw:
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - created > SESSION_TTL:
                path.unlink(missing_ok=True)
                return None
        except ValueError:
            pass
    if not (data.get("access_token") or "").strip():
        return None
    return data


def delete_session(session_id: str) -> None:
    if not session_id:
        return
    _session_path(session_id).unlink(missing_ok=True)
