import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import mailer

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
REQUESTED_ROLE = "editor"


def _requests_dir() -> Path:
    directory = DATA_DIR / "access_requests"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _request_path(token: str) -> Path:
    return _requests_dir() / f"{token}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def create_or_refresh_request(
    *,
    doc_id: str,
    doc_title: str,
    requester_email: str,
    owner_email: str,
) -> dict:
    requester = _normalize_email(requester_email)
    owner = _normalize_email(owner_email)
    if not requester or not owner:
        raise ValueError("requester_email and owner_email are required")
    if not mailer.is_deliverable_email(owner):
        raise ValueError("Document owner does not have a deliverable email address")
    if requester == owner:
        raise ValueError("owner cannot request access to their own document")

    existing = find_pending_for(doc_id, requester)
    if existing:
        record = existing
        record["updated_at"] = _now_iso()
        record["doc_title"] = (doc_title or record.get("doc_title") or doc_id).strip()
    else:
        token = secrets.token_urlsafe(32)
        record = {
            "token": token,
            "doc_id": doc_id,
            "doc_title": (doc_title or doc_id).strip(),
            "requester_email": requester,
            "owner_email": owner,
            "requested_role": REQUESTED_ROLE,
            "status": "pending",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "resolved_at": None,
            "resolved_by": None,
        }

    with open(_request_path(record["token"]), "w") as handle:
        json.dump(record, handle, indent=2)
    return record


def get_request(token: str) -> dict | None:
    path = _request_path((token or "").strip())
    if not path.is_file():
        return None
    try:
        with open(path) as handle:
            return json.load(handle)
    except Exception:
        return None


def find_pending_for(doc_id: str, requester_email: str) -> dict | None:
    requester = _normalize_email(requester_email)
    for path in sorted(_requests_dir().glob("*.json")):
        try:
            with open(path) as handle:
                record = json.load(handle)
        except Exception:
            continue
        if (
            record.get("status") == "pending"
            and record.get("doc_id") == doc_id
            and _normalize_email(record.get("requester_email", "")) == requester
        ):
            return record
    return None


def update_request_status(
    token: str,
    *,
    status: str,
    resolved_by: str,
) -> dict | None:
    record = get_request(token)
    if not record:
        return None
    record["status"] = status
    record["resolved_by"] = _normalize_email(resolved_by)
    record["resolved_at"] = _now_iso()
    record["updated_at"] = record["resolved_at"]
    with open(_request_path(token), "w") as handle:
        json.dump(record, handle, indent=2)
    return record
