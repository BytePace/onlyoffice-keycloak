import json
import os
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
_doc_locks: dict[str, threading.Lock] = {}
_locks_mutex = threading.Lock()


def _docs_dir() -> Path:
    d = DATA_DIR / "docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(doc_id: str) -> Path:
    return _docs_dir() / f"{doc_id}.meta.json"

def get_doc_lock(doc_id: str) -> threading.Lock:
    with _locks_mutex:
        if doc_id not in _doc_locks:
            _doc_locks[doc_id] = threading.Lock()
        return _doc_locks[doc_id]


def list_documents() -> list[dict]:
    docs = []
    for meta_file in sorted(_docs_dir().glob("*.meta.json")):
        try:
            with open(meta_file) as f:
                docs.append(json.load(f))
        except Exception:
            pass
    return sorted(docs, key=lambda d: d.get("created_at", ""))


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def get_doc_role(meta: dict, user_email: str) -> str | None:
    """
    Returns one of: owner/editor/viewer or None if no access.
    """
    email = _normalize_email(user_email)
    owner = _normalize_email(meta.get("owner_email", ""))
    if owner and email == owner:
        return "owner"

    shared_with = meta.get("shared_with") or {}
    role = shared_with.get(email)
    if role in {"viewer", "editor"}:
        return role
    return None


def can_read(meta: dict, user_email: str) -> bool:
    return get_doc_role(meta, user_email) in {"owner", "editor", "viewer"}


def can_write(meta: dict, user_email: str) -> bool:
    return get_doc_role(meta, user_email) in {"owner", "editor"}


def list_documents_for_user(user_email: str) -> list[dict]:
    return [d for d in list_documents() if can_read(d, user_email)]


def create_document(title: str, owner_email: str, nextcloud_path: str) -> dict:
    doc_id = str(uuid.uuid4())
    meta = {
        "id": doc_id,
        "title": title,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "owner_email": _normalize_email(owner_email),
        "shared_with": {},
        "nextcloud_path": nextcloud_path,
    }
    with open(_meta_path(doc_id), "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta


def get_document_meta(doc_id: str) -> dict | None:
    p = _meta_path(doc_id)
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def save_document_meta(doc_id: str, meta: dict) -> None:
    with open(_meta_path(doc_id), "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def share_document(doc_id: str, email: str, role: str) -> dict:
    meta = get_document_meta(doc_id)
    if not meta:
        raise FileNotFoundError(doc_id)

    role = (role or "").strip().lower()
    if role not in {"viewer", "editor"}:
        raise ValueError("role must be viewer or editor")

    owner_email = _normalize_email(meta.get("owner_email", ""))
    target_email = _normalize_email(email)
    if not target_email:
        raise ValueError("email is required")
    if owner_email and target_email == owner_email:
        return meta

    shared_with = meta.get("shared_with")
    if not isinstance(shared_with, dict):
        shared_with = {}
    shared_with[target_email] = role
    meta["shared_with"] = shared_with
    save_document_meta(doc_id, meta)
    return meta


def list_shares(doc_id: str) -> list[dict]:
    meta = get_document_meta(doc_id)
    if not meta:
        raise FileNotFoundError(doc_id)
    shared_with = meta.get("shared_with")
    if not isinstance(shared_with, dict):
        return []
    items = [{"email": k, "role": v} for k, v in shared_with.items() if v in {"viewer", "editor"}]
    return sorted(items, key=lambda x: x["email"])


def revoke_share(doc_id: str, email: str) -> dict:
    meta = get_document_meta(doc_id)
    if not meta:
        raise FileNotFoundError(doc_id)
    shared_with = meta.get("shared_with")
    if not isinstance(shared_with, dict):
        shared_with = {}
    email_norm = _normalize_email(email)
    if email_norm in shared_with:
        del shared_with[email_norm]
        meta["shared_with"] = shared_with
        save_document_meta(doc_id, meta)
    return meta
