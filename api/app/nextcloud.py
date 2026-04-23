import os
import posixpath
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx
import openpyxl

NEXTCLOUD_BASE_URL = os.getenv("NEXTCLOUD_BASE_URL", "").rstrip("/")
NEXTCLOUD_FILES_DIR = os.getenv("NEXTCLOUD_FILES_DIR", "SSA Forms").strip("/") or "SSA Forms"


def _auth_headers(access_token: str) -> dict[str, str]:
    token = (access_token or "").strip()
    if not token:
        raise RuntimeError("Missing access token for Nextcloud operation")
    return {"Authorization": f"Bearer {token}"}


def _webdav_base(user_id: str) -> str:
    return f"{NEXTCLOUD_BASE_URL}/remote.php/dav/files/{quote(user_id, safe='')}"


def _ocs_base() -> str:
    return f"{NEXTCLOUD_BASE_URL}/ocs/v2.php"


def _service_relative_path(file_name: str) -> str:
    return "/" + posixpath.join(NEXTCLOUD_FILES_DIR, file_name)


def _webdav_url(relative_path: str, user_id: str) -> str:
    relative = relative_path.lstrip("/")
    encoded_parts = [quote(part, safe="") for part in relative.split("/") if part]
    return f"{_webdav_base(user_id)}/{'/'.join(encoded_parts)}"


def _sanitize_file_name(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", (title or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Untitled"


def _json_meta(response: httpx.Response) -> dict:
    response.raise_for_status()
    payload = response.json()
    meta = payload.get("ocs", {}).get("meta", {})
    status_code = int(meta.get("statuscode", 999))
    if status_code != 100:
        message = meta.get("message") or "OCS request failed"
        raise httpx.HTTPStatusError(message, request=response.request, response=response)
    return payload.get("ocs", {}).get("data") or {}


async def _current_user_id(access_token: str) -> str:
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
        user_id = (data.get("id") or "").strip()
        if not user_id:
            raise RuntimeError("Could not resolve Nextcloud user id from token")
        return user_id


async def ensure_storage_folder(access_token: str) -> None:
    if not NEXTCLOUD_BASE_URL:
        raise RuntimeError("NEXTCLOUD_BASE_URL is not configured")
    folder_path = "/" + NEXTCLOUD_FILES_DIR
    headers = _auth_headers(access_token)
    user_id = await _current_user_id(access_token)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        head = await client.request("PROPFIND", _webdav_url(folder_path, user_id), headers={**headers, "Depth": "0"})
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
    await ensure_storage_folder(access_token)
    base_name = _sanitize_file_name(title)
    suffix = ".xlsx"
    candidate = f"{base_name}{suffix}"
    index = 1
    while await file_exists(_service_relative_path(candidate), access_token):
        candidate = f"{base_name} ({index}){suffix}"
        index += 1
    return _service_relative_path(candidate)


async def upload_bytes(relative_path: str, content: bytes, access_token: str) -> None:
    headers = _auth_headers(access_token)
    user_id = await _current_user_id(access_token)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.put(
            _webdav_url(relative_path, user_id),
            content=content,
            headers={
                **headers,
                "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
        )
        response.raise_for_status()


async def download_bytes(relative_path: str, access_token: str) -> bytes:
    headers = _auth_headers(access_token)
    user_id = await _current_user_id(access_token)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(_webdav_url(relative_path, user_id), headers=headers)
        response.raise_for_status()
        return response.content


async def create_empty_workbook(relative_path: str, access_token: str) -> None:
    workbook = openpyxl.Workbook()
    if "Sheet" in workbook.sheetnames:
        del workbook["Sheet"]
    with tempfile.NamedTemporaryFile(suffix=".xlsx") as tmp:
        workbook.save(tmp.name)
        tmp.seek(0)
        await upload_bytes(relative_path, tmp.read(), access_token)


def _share_permissions(role: str) -> str:
    return "3" if role == "editor" else "1"


async def create_user_share(relative_path: str, share_with: str, role: str, access_token: str) -> str | None:
    if not share_with:
        return None
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
                "shareWith": share_with,
                "permissions": _share_permissions(role),
            },
        )
        data = _json_meta(response)
        share_id = data.get("id")
        return str(share_id) if share_id is not None else None


async def revoke_user_share(relative_path: str, share_with: str, access_token: str) -> None:
    if not share_with:
        return
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


def title_from_relative_path(relative_path: str) -> str:
    return Path(relative_path).stem
