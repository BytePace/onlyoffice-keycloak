import hashlib
import json
import os
import time

from jose import jwt

ONLYOFFICE_JWT_SECRET = os.getenv("ONLYOFFICE_JWT_SECRET", "")
ONLYOFFICE_DOCS_EXTERNAL_URL = os.getenv("ONLYOFFICE_DOCS_EXTERNAL_URL", "")


def _doc_key(doc_id: str) -> str:
    """Stable key that rotates hourly so OnlyOffice fetches fresh file after save."""
    hour_slot = int(time.time()) // 3600
    return hashlib.sha256(f"{doc_id}:{hour_slot}".encode()).hexdigest()[:20]


def build_editor_config(
    doc_id: str,
    title: str,
    user_email: str,
    file_url: str,
    callback_url: str,
) -> dict:
    config = {
        "document": {
            "fileType": "xlsx",
            "key": _doc_key(doc_id),
            "title": f"{title}.xlsx",
            "url": file_url,
            "permissions": {
                "edit": True,
                "download": True,
                "print": True,
            },
        },
        "documentType": "cell",
        "editorConfig": {
            "callbackUrl": callback_url,
            "user": {"id": user_email, "name": user_email},
            "lang": "en",
            "mode": "edit",
        },
    }

    if ONLYOFFICE_JWT_SECRET:
        token = jwt.encode({"payload": config}, ONLYOFFICE_JWT_SECRET, algorithm="HS256")
        config["token"] = token

    return config


def render_editor_html(config: dict) -> str:
    docs_url = ONLYOFFICE_DOCS_EXTERNAL_URL.rstrip("/")
    config_json = json.dumps(config, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OnlyOffice</title>
  <style>
    html, body, #editor {{ margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; }}
  </style>
</head>
<body>
  <div id="editor"></div>
  <script src="{docs_url}/web-apps/apps/api/documents/api.js"></script>
  <script>
    var docEditor = new DocsAPI.DocEditor("editor", {config_json});
  </script>
</body>
</html>"""
