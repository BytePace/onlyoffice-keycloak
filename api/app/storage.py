import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))


def _docs_dir() -> Path:
    d = DATA_DIR / "docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _meta_path(doc_id: str) -> Path:
    return _docs_dir() / f"{doc_id}.meta.json"


def get_xlsx_path(doc_id: str) -> Path:
    return _docs_dir() / f"{doc_id}.xlsx"


def list_documents() -> list[dict]:
    docs = []
    for meta_file in sorted(_docs_dir().glob("*.meta.json")):
        try:
            with open(meta_file) as f:
                docs.append(json.load(f))
        except Exception:
            pass
    return sorted(docs, key=lambda d: d.get("created_at", ""))


def create_document(title: str) -> dict:
    doc_id = str(uuid.uuid4())
    meta = {
        "id": doc_id,
        "title": title,
        "created_at": datetime.now(timezone.utc).isoformat(),
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
