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


def user_identities(user: dict) -> set[str]:
    """All JWT identities used to match owner/shared_with (email, username, sub, NC user id)."""
    ids: set[str] = set()
    for key in ("email", "preferred_username", "sub", "nextcloud_user_id"):
        value = _normalize_email(str(user.get(key) or ""))
        if value:
            ids.add(value)
    return ids


def get_doc_role(meta: dict, user: dict | str) -> str | None:
    """
    Returns one of: owner/editor/viewer or None if no access.
    Accepts a JWT claims dict or a single normalized email string (legacy).
    """
    identities = user_identities(user) if isinstance(user, dict) else {_normalize_email(user)}
    if not identities:
        return None

    owner = _normalize_email(meta.get("owner_email", ""))
    if owner and owner in identities:
        return "owner"

    shared_with = meta.get("shared_with") or {}
    if not isinstance(shared_with, dict):
        return None
    for shared_email, role in shared_with.items():
        if _normalize_email(shared_email) in identities and role in {"viewer", "editor"}:
            return role
    return None


def can_read(meta: dict, user: dict | str) -> bool:
    return get_doc_role(meta, user) in {"owner", "editor", "viewer"}


def can_write(meta: dict, user: dict | str) -> bool:
    return get_doc_role(meta, user) in {"owner", "editor"}


def list_documents_for_user(user: dict | str) -> list[dict]:
    return [d for d in list_documents() if can_read(d, user)]


def find_document_by_nextcloud_file_id(file_id: str) -> dict | None:
    needle = (file_id or "").strip()
    if not needle:
        return None
    for meta in list_documents():
        if (meta.get("nextcloud_file_id") or "").strip() == needle:
            return meta
    return None


def find_document_by_nextcloud_path(path: str) -> dict | None:
    needle = _normalize_nc_path(path)
    if not needle:
        return None
    for meta in list_documents():
        if _normalize_nc_path(meta.get("nextcloud_path") or "") == needle:
            return meta
    return None


def _normalize_nc_path(path: str) -> str:
    cleaned = (path or "").strip()
    if not cleaned:
        return ""
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned


def create_document(
    title: str,
    owner_email: str,
    nextcloud_path: str,
    nextcloud_file_id: str = "",
) -> dict:
    doc_id = str(uuid.uuid4())
    meta = {
        "id": doc_id,
        "title": title,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "owner_email": _normalize_email(owner_email),
        "shared_with": {},
        "nextcloud_path": nextcloud_path,
        "nextcloud_file_id": (nextcloud_file_id or "").strip(),
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


def get_content_revision(meta: dict | None) -> int:
    if not meta:
        return 0
    try:
        return max(0, int(meta.get("content_revision") or 0))
    except (TypeError, ValueError):
        return 0


def bump_content_revision(doc_id: str) -> int:
    """Increment revision after external API writes so open browser editors can reload."""
    meta = get_document_meta(doc_id)
    if not meta:
        raise FileNotFoundError(doc_id)
    revision = get_content_revision(meta) + 1
    meta = dict(meta)
    meta["content_revision"] = revision
    save_document_meta(doc_id, meta)
    return revision


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


def ensure_nextcloud_path(doc_id: str, nextcloud_path: str) -> dict | None:
    meta = get_document_meta(doc_id)
    if not meta:
        return None
    path = _normalize_nc_path(nextcloud_path)
    if not path:
        return meta
    current = _normalize_nc_path(meta.get("nextcloud_path") or "")
    if current:
        return meta
    meta = dict(meta)
    meta["nextcloud_path"] = path
    save_document_meta(doc_id, meta)
    return meta


def ensure_owner_email(doc_id: str, owner_label: str) -> dict | None:
    """Fill owner_email when missing (e.g. shared docs synced with Nextcloud uid only)."""
    meta = get_document_meta(doc_id)
    if not meta:
        return None
    label = _normalize_email(owner_label)
    if not label:
        return meta
    current = _normalize_email(meta.get("owner_email", ""))
    if current:
        return meta
    meta = dict(meta)
    meta["owner_email"] = label
    save_document_meta(doc_id, meta)
    return meta


def delete_document(doc_id: str) -> None:
    p = _meta_path(doc_id)
    if p.exists():
        p.unlink()


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
