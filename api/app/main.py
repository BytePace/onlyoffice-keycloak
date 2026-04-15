import os

import httpx
from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi import FastAPI

from .auth import get_current_user
from .models import AddRecordsRequest, CreateDocRequest, CreateTablesRequest
from . import onlyoffice, spreadsheet, storage

app = FastAPI(title="OnlyOffice Spreadsheet API", version="1.0.0")

API_EXTERNAL_URL = os.getenv("API_EXTERNAL_URL", "")


@app.get("/health")
async def health():
    return {"status": "ok"}


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
