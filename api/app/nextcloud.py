import logging
import os
import posixpath
import re
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

_FILE_ID_PATTERN = re.compile(
    r"<(?:oc|nc):fileid[^>]*>(\d+)</(?:oc|nc):fileid>",
    re.IGNORECASE,
)
_PROPFIND_FILE_ID = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <oc:fileid />
    <nc:fileid />
  </d:prop>
</d:propfind>
"""
_PROPFIND_LIST_WORKBOOKS = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <d:displayname />
    <d:resourcetype />
    <d:getcontenttype />
    <oc:fileid />
    <nc:fileid />
  </d:prop>
</d:propfind>
"""
_SKIP_DAV_FOLDER_NAMES = frozenset(
    {".trash", ".trashbin", "talk", "skeleton", "templates", "files_trashbin"}
)
_DAV_MAX_VISITED_DIRS = int(os.getenv("NEXTCLOUD_DAV_MAX_DIRS", "500"))
_DAV_MAX_FOLDER_DEPTH = int(os.getenv("NEXTCLOUD_DAV_MAX_DEPTH", "16"))
_RESPONSE_BLOCK = re.compile(r"<d:response[^>]*>(.*?)</d:response>", re.IGNORECASE | re.DOTALL)
_HREF_PATTERN = re.compile(r"<d:href>([^<]+)</d:href>", re.IGNORECASE)

import httpx
import openpyxl

NEXTCLOUD_BASE_URL = os.getenv("NEXTCLOUD_BASE_URL", "").rstrip("/")
# Empty = user's WebDAV root (Files home). Set to a folder name to scope new spreadsheets there.
NEXTCLOUD_FILES_DIR = os.getenv("NEXTCLOUD_FILES_DIR", "").strip("/")
# Used when NEXTCLOUD_FILES_DIR is already mounted from another user's share (same folder name).
NEXTCLOUD_OWN_FILES_DIR_FALLBACK = (
    os.getenv("NEXTCLOUD_OWN_FILES_DIR_FALLBACK") or "SSA Forms (My files)"
).strip("/") or "SSA Forms (My files)"
_MOUNT_ROOT_PATTERN = re.compile(
    r"<nc:is-mount-root[^>]*>\s*true\s*</nc:is-mount-root>",
    re.IGNORECASE,
)
_PROPFIND_MOUNT_CHECK = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <nc:is-mount-root />
    <d:resourcetype />
  </d:prop>
</d:propfind>
"""
_PROPFIND_SHARE_MOUNTS = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <nc:is-mount-root />
    <d:resourcetype />
    <d:displayname />
    <oc:owner-display-name />
  </d:prop>
</d:propfind>
"""
_COLLECTION_PATTERN = re.compile(r"<d:collection\b", re.IGNORECASE)
_DISPLAYNAME_PATTERN = re.compile(
    r"<d:displayname[^>]*>([^<]*)</d:displayname>",
    re.IGNORECASE,
)
_OWNER_DISPLAY_PATTERN = re.compile(
    r"<oc:owner-display-name[^>]*>([^<]*)</oc:owner-display-name>",
    re.IGNORECASE,
)


def _auth_headers(access_token: str) -> dict[str, str]:
    token = (access_token or "").strip()
    if not token:
        raise RuntimeError("Missing access token for Nextcloud operation")
    return {"Authorization": f"Bearer {token}"}


def _webdav_base(user_id: str) -> str:
    return f"{NEXTCLOUD_BASE_URL}/remote.php/dav/files/{quote(user_id, safe='')}"


def _ocs_base() -> str:
    return f"{NEXTCLOUD_BASE_URL}/ocs/v2.php"


def _service_relative_path(file_name: str, storage_root: str | None = None) -> str:
    root = (storage_root if storage_root is not None else NEXTCLOUD_FILES_DIR).strip("/")
    if root:
        return "/" + posixpath.join(root, file_name)
    return "/" + file_name.lstrip("/")


def _webdav_url(relative_path: str, user_id: str) -> str:
    relative = relative_path.lstrip("/")
    encoded_parts = [quote(part, safe="") for part in relative.split("/") if part]
    return f"{_webdav_base(user_id)}/{'/'.join(encoded_parts)}"


def _sanitize_file_name(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", (title or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Untitled"


def _json_meta(response: httpx.Response):
    response.raise_for_status()
    payload = response.json()
    meta = payload.get("ocs", {}).get("meta", {})
    status_code = int(meta.get("statuscode", 999))
    # Nextcloud OCS APIs typically return 100 for success, but some installations
    # report 200 with status="ok" for successful calls as well.
    if status_code not in (100, 200):
        message = meta.get("message") or "OCS request failed"
        raise httpx.HTTPStatusError(message, request=response.request, response=response)
    if "data" not in payload.get("ocs", {}):
        return {}
    return payload["ocs"]["data"]


def _ensure_http_success(response: httpx.Response, *, action: str) -> None:
    """Accept modern Nextcloud responses that omit a classic OCS envelope."""
    response.raise_for_status()
    if response.status_code not in (200, 201, 204):
        raise httpx.HTTPStatusError(
            f"{action} failed with HTTP {response.status_code}",
            request=response.request,
            response=response,
        )
    content = (response.text or "").strip()
    if not content:
        return
    try:
        payload = response.json()
    except Exception:
        return
    if "ocs" not in payload:
        return
    meta = payload.get("ocs", {}).get("meta", {})
    status_code = int(meta.get("statuscode", 100))
    if status_code in (100, 200):
        return
    message = meta.get("message") or f"{action} failed"
    raise httpx.HTTPStatusError(message, request=response.request, response=response)


def _folder_mount_name_from_share(share: dict) -> str:
    file_target = str(share.get("file_target") or "").strip().strip("/")
    if file_target:
        return file_target.split("/")[0]
    label = str(share.get("label") or "").strip().strip("/")
    if label:
        return label.split("/")[0]
    raw_path = str(share.get("path") or "").strip().strip("/")
    if raw_path:
        return raw_path.split("/")[0]
    return ""


def summarize_share_for_diagnostics(share: dict) -> dict:
    return {
        "id": share.get("id"),
        "item_type": share.get("item_type"),
        "path": share.get("path"),
        "file_target": share.get("file_target"),
        "label": share.get("label"),
        "uid_owner": share.get("uid_owner"),
        "status": share.get("status"),
        "share_type": share.get("share_type"),
    }


async def current_user_profile(access_token: str) -> dict:
    headers = {
        **_auth_headers(access_token),
        "OCS-APIRequest": "true",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(
            f"{_ocs_base()}/cloud/user",
            params={"format": "json"},
            headers=headers,
        )
        data = _json_meta(response)
        if not isinstance(data, dict):
            return {}
        return data


async def _current_user_id(access_token: str) -> str:
    data = await current_user_profile(access_token)
    user_id = (data.get("id") or "").strip()
    if not user_id:
        raise RuntimeError("Could not resolve Nextcloud user id from token")
    return user_id


async def _list_shares_with_me_raw(access_token: str) -> list[dict]:
    headers = {
        **_auth_headers(access_token),
        "OCS-APIRequest": "true",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(
            f"{_ocs_base()}/apps/files_sharing/api/v1/shares",
            params={"format": "json", "shared_with_me": "true"},
            headers=headers,
        )
        data = _json_meta(response)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


async def list_pending_shares(access_token: str) -> list[dict]:
    """User/group shares waiting for recipient acceptance (not visible in Files yet)."""
    headers = {
        **_auth_headers(access_token),
        "OCS-APIRequest": "true",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(
            f"{_ocs_base()}/apps/files_sharing/api/v1/shares/pending",
            params={"format": "json"},
            headers=headers,
        )
        data = _json_meta(response)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


async def accept_pending_share(share_id: str, access_token: str) -> None:
    share_id = (share_id or "").strip()
    if not share_id:
        raise ValueError("share id is required")
    headers = {
        **_auth_headers(access_token),
        "OCS-APIRequest": "true",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.post(
            f"{_ocs_base()}/apps/files_sharing/api/v1/shares/pending/{quote(share_id, safe='')}",
            params={"format": "json"},
            headers=headers,
        )
        _ensure_http_success(response, action="Accept pending share")


async def accept_all_pending_shares(access_token: str) -> tuple[int, list[str]]:
    """
    Accept every pending user/group share for the current account.

    Nextcloud can require manual acceptance before shares appear in Files and in
    shared_with_me; the mobile app auto-accepts so User2 does not need the web UI.
    """
    accepted = 0
    errors: list[str] = []
    for share in await list_pending_shares(access_token):
        share_id = str(share.get("id") or "").strip()
        if not share_id:
            errors.append("pending share without id")
            continue
        try:
            await accept_pending_share(share_id, access_token)
            accepted += 1
            logger.info(
                "Accepted pending share id=%s path=%s from=%s",
                share_id,
                share.get("path") or share.get("file_target") or "",
                share.get("uid_owner") or "",
            )
        except Exception as exc:
            message = f"id={share_id}: {exc}"
            errors.append(message)
            logger.warning("Failed to accept pending share %s", message)
    return accepted, errors


async def _shared_folder_mount_names(access_token: str) -> set[str]:
    """Top-level folder names received via Nextcloud share (mounted into the user's Files)."""
    names: set[str] = set()
    for share in await _list_shares_with_me_raw(access_token):
        if str(share.get("item_type") or "").lower() != "folder":
            continue
        file_target = str(share.get("file_target") or "").strip().strip("/")
        if file_target:
            names.add(file_target.split("/")[0])
        label = str(share.get("label") or "").strip().strip("/")
        if label:
            names.add(label.split("/")[0])
    return names


async def folder_is_received_share_mount(relative_path: str, access_token: str) -> bool:
    """True when path is another user's folder mounted into this account (not writable own storage)."""
    normalized = _normalize_nc_path(relative_path)
    if not normalized:
        return False
    user_id = await _current_user_id(access_token)
    headers = {
        **_auth_headers(access_token),
        "Depth": "0",
        "Content-Type": "application/xml",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.request(
            "PROPFIND",
            _webdav_url(normalized, user_id),
            content=_PROPFIND_MOUNT_CHECK,
            headers=headers,
        )
        if response.status_code in (200, 207) and _MOUNT_ROOT_PATTERN.search(response.text):
            return True
    folder_name = normalized.strip("/").split("/")[0]
    return folder_name in await _shared_folder_mount_names(access_token)


def _parse_propfind_share_mount_folders(xml_text: str, user_id: str) -> list[dict]:
    """Top-level folder mounts created by Nextcloud for received shares."""
    user_prefix = f"/remote.php/dav/files/{quote(user_id, safe='')}/"
    mounts: list[dict] = []
    seen: set[str] = set()
    for block in _RESPONSE_BLOCK.findall(xml_text):
        if not _MOUNT_ROOT_PATTERN.search(block) or not _COLLECTION_PATTERN.search(block):
            continue
        href_match = _HREF_PATTERN.search(block)
        if not href_match:
            continue
        href = unquote(href_match.group(1).strip()).rstrip("/")
        if user_prefix not in href:
            continue
        relative = href.split(user_prefix, 1)[-1].strip("/")
        if not relative or relative in seen:
            continue
        seen.add(relative)
        display_match = _DISPLAYNAME_PATTERN.search(block)
        display_name = display_match.group(1).strip() if display_match else ""
        owner_match = _OWNER_DISPLAY_PATTERN.search(block)
        owner_label = owner_match.group(1).strip() if owner_match else ""
        mount_name = relative.split("/")[0]
        name = display_name or mount_name
        mounts.append(
            {
                "name": name,
                "mount_path": _normalize_nc_path(relative),
                "parent_path": "",
                "owner_email": owner_label,
            }
        )
    return mounts


async def list_shared_folder_mounts_for_picker(access_token: str) -> list[dict]:
    """
    Received folder shares as separate picker entries.

    When the recipient already has their own \"SSA Forms\" folder, Nextcloud mounts
    User1's share under another name (e.g. \"SSA Forms (2)\"). WebDAV mount roots and
    OCS folder shares are merged so the iOS picker can show both trees.
    """
    by_name: dict[str, dict] = {}
    user_id = await _current_user_id(access_token)
    headers = {
        **_auth_headers(access_token),
        "Depth": "1",
        "Content-Type": "application/xml",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.request(
                "PROPFIND",
                f"{_webdav_base(user_id)}/",
                content=_PROPFIND_SHARE_MOUNTS,
                headers=headers,
            )
            if response.status_code in (200, 207):
                for item in _parse_propfind_share_mount_folders(response.text, user_id):
                    key = item["name"]
                    by_name[key] = item
    except Exception as exc:
        logger.warning("list_shared_folder_mounts WebDAV failed: %s", exc)

    for share in await _list_shares_with_me_raw(access_token):
        if str(share.get("item_type") or "").lower() != "folder":
            continue
        file_target = str(share.get("file_target") or "").strip().strip("/")
        mount_name = _folder_mount_name_from_share(share)
        if not mount_name:
            continue
        owner_id = str(share.get("uid_owner") or "").strip()
        entry = by_name.get(mount_name, {
            "name": mount_name,
            "mount_path": _normalize_nc_path(file_target) if file_target else "",
            "parent_path": "",
            "owner_email": owner_id,
        })
        if owner_id and not entry.get("owner_email"):
            entry["owner_email"] = owner_id
        by_name[mount_name] = entry

    for share in await list_pending_shares(access_token):
        if str(share.get("item_type") or "").lower() != "folder":
            continue
        mount_name = _folder_mount_name_from_share(share)
        if not mount_name:
            continue
        owner_id = str(share.get("uid_owner") or "").strip()
        entry = by_name.get(mount_name, {
            "name": mount_name,
            "mount_path": "",
            "parent_path": "",
            "owner_email": owner_id,
            "pending": True,
        })
        if owner_id and not entry.get("owner_email"):
            entry["owner_email"] = owner_id
        entry["pending"] = True
        by_name[mount_name] = entry

    own_primary = NEXTCLOUD_FILES_DIR
    results = []
    for entry in by_name.values():
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        # Skip a configured storage folder name when it is the user's own directory, not a received mount.
        if own_primary and name == own_primary and not await folder_is_received_share_mount(
            "/" + own_primary, access_token
        ):
            continue
        results.append(entry)
    return sorted(results, key=lambda item: (item.get("name") or "").lower())


async def resolve_own_storage_root(access_token: str) -> str:
    """
    Root folder for NEW spreadsheets created by the mobile app for this user.

    By default this is the user's WebDAV root. When NEXTCLOUD_FILES_DIR is set and that
    path is a received share mount, a separate fallback folder is used instead.
    """
    primary_name = NEXTCLOUD_FILES_DIR.strip("/")
    if not primary_name:
        return "/"
    primary = "/" + primary_name
    if await folder_is_received_share_mount(primary, access_token):
        fallback = "/" + NEXTCLOUD_OWN_FILES_DIR_FALLBACK
        logger.info(
            "Using fallback storage root %s (received share mount at %s)",
            fallback,
            primary,
        )
        await ensure_storage_folder(access_token, fallback)
        return fallback
    await ensure_storage_folder(access_token, primary)
    return primary


async def ensure_storage_folder(access_token: str, folder_path: str | None = None) -> None:
    if not NEXTCLOUD_BASE_URL:
        raise RuntimeError("NEXTCLOUD_BASE_URL is not configured")
    default_path = ("/" + NEXTCLOUD_FILES_DIR) if NEXTCLOUD_FILES_DIR else "/"
    folder_path = _normalize_nc_path(folder_path or default_path)
    if folder_path in ("/", ""):
        return
    headers = _auth_headers(access_token)
    user_id = await _current_user_id(access_token)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        head = await client.request(
            "PROPFIND",
            _webdav_url(folder_path, user_id),
            headers={**headers, "Depth": "0"},
        )
        if head.status_code in (200, 207):
            return
        if head.status_code != 404:
            head.raise_for_status()
        response = await client.request("MKCOL", _webdav_url(folder_path, user_id), headers=headers)
        if response.status_code not in (201, 405):
            response.raise_for_status()


async def file_exists(relative_path: str, access_token: str) -> bool:
    headers = _auth_headers(access_token)
    user_id = await _current_user_id(access_token)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.request("HEAD", _webdav_url(relative_path, user_id), headers=headers)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True


async def reserve_document_path(title: str, access_token: str) -> str:
    storage_root = await resolve_own_storage_root(access_token)
    storage_root = storage_root.strip("/")
    base_name = _sanitize_file_name(title)
    suffix = ".xlsx"
    candidate = f"{base_name}{suffix}"
    index = 1
    while await file_exists(_service_relative_path(candidate, storage_root=storage_root), access_token):
        candidate = f"{base_name} ({index}){suffix}"
        index += 1
    return _service_relative_path(candidate, storage_root=storage_root)


async def upload_bytes(relative_path: str, content: bytes, access_token: str, *, file_id: str = "") -> None:
    resolved = await resolve_workbook_access_path(relative_path, access_token, file_id=file_id)
    headers = _auth_headers(access_token)
    user_id = await _current_user_id(access_token)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.put(
            _webdav_url(resolved, user_id),
            content=content,
            headers={
                **headers,
                "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
        )
        response.raise_for_status()


async def resolve_workbook_access_path(
    relative_path: str,
    access_token: str,
    *,
    file_id: str = "",
) -> str:
    """Pick a WebDAV path the current user can read (owner path or shared-with-me path)."""
    path = _normalize_nc_path(relative_path)
    if path and await file_exists(path, access_token):
        return path
    needle_id = (file_id or "").strip()
    for share in await list_accessible_workbooks(access_token):
        share_path = share.get("path") or ""
        if not share_path:
            continue
        if needle_id and share.get("file_id") == needle_id:
            return share_path
        if path and _normalize_nc_path(share_path) == path:
            return share_path
    return path or relative_path


async def download_bytes(relative_path: str, access_token: str, *, file_id: str = "") -> bytes:
    resolved = await resolve_workbook_access_path(relative_path, access_token, file_id=file_id)
    headers = _auth_headers(access_token)
    user_id = await _current_user_id(access_token)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(_webdav_url(resolved, user_id), headers=headers)
        response.raise_for_status()
        return response.content


async def create_empty_workbook(relative_path: str, access_token: str) -> None:
    workbook = openpyxl.Workbook()
    # Keep at least one visible sheet, otherwise openpyxl cannot save workbook.
    worksheet = workbook.active
    worksheet.title = "Sheet1"
    with tempfile.NamedTemporaryFile(suffix=".xlsx") as tmp:
        workbook.save(tmp.name)
        tmp.seek(0)
        await upload_bytes(relative_path, tmp.read(), access_token)


def _share_permissions(role: str) -> str:
    return "3" if role == "editor" else "1"


def _normalize_share_identity(value: str) -> str:
    return (value or "").strip().lower()


async def _ocs_user_profile(user_id: str, access_token: str) -> dict:
    headers = {
        **_auth_headers(access_token),
        "OCS-APIRequest": "true",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(
            f"{_ocs_base()}/cloud/users/{quote(user_id, safe='')}",
            params={"format": "json"},
            headers=headers,
        )
        data = _json_meta(response)
        return data if isinstance(data, dict) else {}


async def resolve_user_id_for_share(share_with: str, access_token: str) -> str:
    """
    Map an email to the Nextcloud account id used in OCS shareWith.
    OIDC users often have a hash userid, not the email string.
    """
    raw = (share_with or "").strip()
    if not raw:
        raise ValueError("share recipient is required")
    if "@" not in raw:
        return raw

    target_email = _normalize_share_identity(raw)
    headers = {
        **_auth_headers(access_token),
        "OCS-APIRequest": "true",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(
            f"{_ocs_base()}/cloud/users",
            params={"format": "json", "search": raw, "limit": 25},
            headers=headers,
        )
        try:
            data = _json_meta(response)
        except Exception as exc:
            logger.warning("Nextcloud user search failed for %s: %s", raw, exc)
            return raw

    user_ids = []
    if isinstance(data, dict):
        user_ids = data.get("users") or []
    elif isinstance(data, list):
        user_ids = data

    for user_id in user_ids:
        uid = str(user_id or "").strip()
        if not uid:
            continue
        profile = await _ocs_user_profile(uid, access_token)
        profile_email = _normalize_share_identity(str(profile.get("email") or ""))
        if profile_email == target_email:
            return uid

    logger.warning(
        "No Nextcloud account id for email %s (search returned %s ids)",
        raw,
        len(user_ids),
    )
    return raw


async def create_user_share(relative_path: str, share_with: str, role: str, access_token: str) -> str | None:
    if not share_with:
        return None
    nc_share_with = await resolve_user_id_for_share(share_with, access_token)
    headers = {
        **_auth_headers(access_token),
        "OCS-APIRequest": "true",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.post(
            f"{_ocs_base()}/apps/files_sharing/api/v1/shares",
            params={"format": "json"},
            headers=headers,
            data={
                "path": relative_path,
                "shareType": "0",
                "shareWith": nc_share_with,
                "permissions": _share_permissions(role),
            },
        )
        data = _json_meta(response)
        share_id = data.get("id")
        return str(share_id) if share_id is not None else None


async def revoke_user_share(relative_path: str, share_with: str, access_token: str) -> None:
    if not share_with:
        return
    share_with = await resolve_user_id_for_share(share_with, access_token)
    headers = {
        **_auth_headers(access_token),
        "OCS-APIRequest": "true",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        shares_resp = await client.get(
            f"{_ocs_base()}/apps/files_sharing/api/v1/shares",
            params={"format": "json", "path": relative_path, "reshares": "true"},
            headers=headers,
        )
        shares_data = _json_meta(shares_resp)
        shares = shares_data if isinstance(shares_data, list) else ([shares_data] if shares_data else [])
        for share in shares:
            if str(share.get("share_type")) != "0":
                continue
            if str(share.get("share_with")) != share_with:
                continue
            share_id = share.get("id")
            if share_id is None:
                continue
            delete_resp = await client.delete(
                f"{_ocs_base()}/apps/files_sharing/api/v1/shares/{share_id}",
                params={"format": "json"},
                headers=headers,
            )
            _json_meta(delete_resp)


def file_name_from_relative_path(relative_path: str) -> str:
    return Path(relative_path).name


def content_disposition_inline(filename: str) -> str:
    """HTTP header safe for Starlette (latin-1) with UTF-8 filename fallback."""
    raw = (filename or "document.xlsx").strip() or "document.xlsx"
    ascii_fallback = re.sub(r"[^\x20-\x7E]+", "_", raw) or "document.xlsx"
    return f"inline; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(raw, safe='')}"


def title_from_relative_path(relative_path: str) -> str:
    return Path(relative_path).stem


def parent_path_from_nextcloud_path(nextcloud_path: str) -> str:
    """
    Parent folder relative to the user's WebDAV root, e.g. "Team/2024".
    Empty string means the file lives directly in the user's Files root.
    """
    normalized = _normalize_nc_path(nextcloud_path)
    if not normalized:
        return ""
    parent = posixpath.dirname(normalized.strip("/"))
    if parent in ("", "."):
        return ""
    return parent.strip("/")


def browser_open_url(file_id: str, nextcloud_path: str = "") -> str:
    """Nextcloud Files deep link that opens the spreadsheet in the integrated editor."""
    normalized = _normalize_nc_path(nextcloud_path)
    if normalized:
        parent = posixpath.dirname(normalized.strip("/"))
        dir_path = f"/{parent}" if parent else "/"
    else:
        dir_path = "/"
    dir_param = quote(dir_path, safe="/")
    return f"{NEXTCLOUD_BASE_URL}/apps/files/files/{file_id}?dir={dir_param}&openfile=true"


def _normalize_nc_path(path: str) -> str:
    cleaned = (path or "").strip()
    if not cleaned:
        return ""
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned


def top_level_folder(relative_path: str) -> str:
    """First path segment under the user's WebDAV root, or empty for root-level files."""
    parts = _normalize_nc_path(relative_path).strip("/").split("/")
    if len(parts) <= 1:
        return ""
    return parts[0]


def permissions_to_role(permissions: int) -> str:
    return "editor" if permissions & 2 else "viewer"


def _permissions_to_role(permissions: int) -> str:
    return permissions_to_role(permissions)


def workbook_access_role(
    share: dict,
    wb_path: str,
    *,
    folder_share_roles: dict[str, str] | None = None,
    shared_mount_names: set[str] | None = None,
) -> str:
    """
    Resolve editor/viewer for a workbook the recipient can see in Nextcloud.

    OCS file shares include permissions; WebDAV listings do not and default to viewer.
    Files inside a received folder mount inherit the folder share role instead.
    """
    folder_share_roles = folder_share_roles or {}
    shared_mount_names = shared_mount_names or set()
    role = (share.get("role") or "").strip().lower()
    owner_id = (share.get("owner_id") or "").strip()
    if owner_id and role in {"editor", "viewer"}:
        return role

    top_folder = top_level_folder(wb_path)
    if top_folder and top_folder in folder_share_roles:
        return folder_share_roles[top_folder]

    if role in {"editor", "viewer"}:
        return role

    if top_folder and top_folder in shared_mount_names:
        return folder_share_roles.get(top_folder, "editor")

    return "viewer"


def _is_spreadsheet_share(share: dict) -> bool:
    if str(share.get("item_type") or "").lower() != "file":
        return False
    path = str(share.get("path") or "")
    name = str(share.get("file_target") or share.get("label") or path)
    mimetype = str(share.get("mimetype") or "").lower()
    return (
        path.lower().endswith(".xlsx")
        or name.lower().endswith(".xlsx")
        or "spreadsheet" in mimetype
        or "officedocument.spreadsheetml" in mimetype
    )


def _relative_path_from_href(href: str, user_id: str) -> str:
    """Path under the user's WebDAV root, e.g. SSA Forms/file.xlsx."""
    decoded = unquote((href or "").strip()).rstrip("/")
    if "://" in decoded:
        marker = "/remote.php/dav/files/"
        idx = decoded.find(marker)
        if idx >= 0:
            decoded = decoded[idx + len(marker) :]
            slash = decoded.find("/")
            if slash >= 0:
                decoded = decoded[slash + 1 :]
            else:
                return ""
    user_prefix = f"/remote.php/dav/files/{quote(user_id, safe='')}/"
    if user_prefix in decoded:
        return decoded.split(user_prefix, 1)[-1].strip("/")
    if decoded.startswith("/"):
        return decoded.strip("/")
    return decoded.strip("/")


def _is_spreadsheet_href(href: str) -> bool:
    lowered = unquote(href or "").lower()
    return lowered.endswith(".xlsx") or lowered.endswith(".xlsm")


def _parse_propfind_directory_listing(
    xml_text: str,
    user_id: str,
    *,
    current_folder: str,
) -> tuple[list[dict], list[str]]:
    """
    Parse PROPFIND Depth:1 listing into xlsx files and immediate subfolder names.
    current_folder is relative to the user's WebDAV root (empty string = root).
    """
    current_folder = (current_folder or "").strip("/")
    workbooks: list[dict] = []
    subfolders: list[str] = []
    seen_folders: set[str] = set()
    for block in _RESPONSE_BLOCK.findall(xml_text):
        href_match = _HREF_PATTERN.search(block)
        if not href_match:
            continue
        relative = _relative_path_from_href(href_match.group(1), user_id)
        if relative == current_folder:
            continue
        if current_folder:
            prefix = current_folder + "/"
            if not relative.startswith(prefix):
                continue
            name = relative[len(prefix) :].split("/")[0]
            if not name:
                continue
            child_path = f"{current_folder}/{name}"
        else:
            name = relative.split("/")[0]
            if not name:
                continue
            child_path = name

        is_collection = bool(_COLLECTION_PATTERN.search(block))
        if _is_spreadsheet_href(href_match.group(1)):
            path = _normalize_nc_path(relative)
            file_id_match = _FILE_ID_PATTERN.search(block)
            workbooks.append(
                {
                    "file_id": file_id_match.group(1) if file_id_match else "",
                    "path": path,
                    "title": title_from_relative_path(path),
                    "role": "viewer",
                    "owner_id": "",
                }
            )
            continue
        if not is_collection:
            continue
        if "/" in (relative[len(current_folder) + 1 :] if current_folder else relative):
            continue
        lowered = name.lower()
        if lowered in _SKIP_DAV_FOLDER_NAMES or name.startswith("."):
            continue
        if child_path not in seen_folders:
            seen_folders.add(child_path)
            subfolders.append(child_path)
    return workbooks, subfolders


def _parse_propfind_workbooks(xml_text: str, user_id: str) -> list[dict]:
    """Legacy flat parser kept for tests and fallback."""
    user_prefix = f"/remote.php/dav/files/{quote(user_id, safe='')}/"
    workbooks: list[dict] = []
    seen: set[str] = set()
    for block in _RESPONSE_BLOCK.findall(xml_text):
        href_match = _HREF_PATTERN.search(block)
        if not href_match:
            continue
        href = unquote(href_match.group(1).strip())
        if not _is_spreadsheet_href(href):
            continue
        if user_prefix not in href and "/remote.php/dav/files/" not in href:
            continue
        relative = _relative_path_from_href(href, user_id)
        path = _normalize_nc_path(relative)
        if not path or path in seen:
            continue
        seen.add(path)
        file_id_match = _FILE_ID_PATTERN.search(block)
        file_id = file_id_match.group(1) if file_id_match else ""
        workbooks.append(
            {
                "file_id": file_id,
                "path": path,
                "title": title_from_relative_path(path),
                "role": "viewer",
                "owner_id": "",
            }
        )
    return workbooks


async def list_user_dav_workbooks(access_token: str) -> list[dict]:
    """
    XLSX files visible in the user's WebDAV tree (own files and mounted shares).

    Nextcloud only returns immediate children for PROPFIND Depth:1, so we walk
    directories recursively (including received share mounts).
    """
    user_id = await _current_user_id(access_token)
    headers = {
        **_auth_headers(access_token),
        "Depth": "1",
        "Content-Type": "application/xml",
    }
    workbooks: list[dict] = []
    seen_paths: set[str] = set()
    queue: list[str] = [""]
    seen_dirs: set[str] = set()
    visited_dirs = 0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        while queue:
            folder_rel = queue.pop(0)
            if folder_rel in seen_dirs:
                continue
            seen_dirs.add(folder_rel)
            visited_dirs += 1
            if visited_dirs > _DAV_MAX_VISITED_DIRS:
                logger.warning(
                    "list_user_dav_workbooks: stopped after %s directories",
                    _DAV_MAX_VISITED_DIRS,
                )
                break
            depth = 0 if not folder_rel else folder_rel.count("/") + 1
            if depth > _DAV_MAX_FOLDER_DEPTH:
                continue

            folder_url = (
                f"{_webdav_base(user_id)}/"
                if not folder_rel
                else _webdav_url(folder_rel, user_id)
            )
            try:
                response = await client.request(
                    "PROPFIND",
                    folder_url,
                    content=_PROPFIND_LIST_WORKBOOKS,
                    headers=headers,
                )
            except Exception as exc:
                logger.warning("PROPFIND failed for %s: %s", folder_url, exc)
                continue
            if response.status_code not in (200, 207):
                logger.warning(
                    "WebDAV PROPFIND for %s returned %s",
                    folder_rel or "/",
                    response.status_code,
                )
                continue

            found_files, subfolders = _parse_propfind_directory_listing(
                response.text,
                user_id,
                current_folder=folder_rel,
            )
            for item in found_files:
                path = item.get("path") or ""
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                workbooks.append(item)
            for subfolder in subfolders:
                if subfolder not in seen_dirs:
                    queue.append(subfolder)

    logger.info(
        "list_user_dav_workbooks: %s spreadsheets across %s folders for %s",
        len(workbooks),
        visited_dirs,
        user_id,
    )
    return workbooks


async def list_accessible_workbooks(access_token: str) -> list[dict]:
    """Merge OCS shared_with_me and WebDAV-visible spreadsheets."""
    merged: dict[str, dict] = {}

    def _key(item: dict) -> str:
        file_id = (item.get("file_id") or "").strip()
        if file_id:
            return f"id:{file_id}"
        return f"path:{item.get('path') or ''}"

    try:
        for item in await list_shares_with_me_workbooks(access_token):
            merged[_key(item)] = item
    except Exception as exc:
        logger.warning("list_shares_with_me_workbooks failed: %s", exc)

    try:
        for item in await list_user_dav_workbooks(access_token):
            merged.setdefault(_key(item), item)
    except Exception as exc:
        logger.warning("list_user_dav_workbooks failed: %s", exc)

    return list(merged.values())


async def list_shares_with_me_workbooks(access_token: str) -> list[dict]:
    """Workbooks shared with the current user via Nextcloud user/group shares (shareType 0/1)."""
    shares = await _list_shares_with_me_raw(access_token)
    workbooks: list[dict] = []
    for share in shares:
        if not isinstance(share, dict) or not _is_spreadsheet_share(share):
            continue
        file_id = str(share.get("file_source") or "").strip()
        if not file_id:
            continue
        raw_path = str(share.get("path") or "").strip()
        file_target = str(share.get("file_target") or "").strip()
        if raw_path:
            path = _normalize_nc_path(raw_path)
        elif file_target:
            path = _normalize_nc_path(file_target)
        else:
            continue
        permissions = int(share.get("permissions") or 1)
        workbooks.append(
            {
                "file_id": file_id,
                "path": path,
                "title": title_from_relative_path(path),
                "role": _permissions_to_role(permissions),
                "owner_id": str(share.get("uid_owner") or "").strip().lower(),
            }
        )
    return workbooks


async def resolve_file_id(relative_path: str, access_token: str) -> str:
    """Resolve Nextcloud numeric file id for a WebDAV path (used in Files app URLs)."""
    user_id = await _current_user_id(access_token)
    headers = {
        **_auth_headers(access_token),
        "Depth": "0",
        "Content-Type": "application/xml",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.request(
            "PROPFIND",
            _webdav_url(relative_path, user_id),
            content=_PROPFIND_FILE_ID,
            headers=headers,
        )
        response.raise_for_status()
    match = _FILE_ID_PATTERN.search(response.text)
    if not match:
        raise RuntimeError(f"Could not resolve Nextcloud file id for {relative_path}")
    return match.group(1)
