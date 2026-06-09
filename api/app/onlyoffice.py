import hashlib
import html
import json
import os

from jose import jwt

ONLYOFFICE_JWT_SECRET = os.getenv("ONLYOFFICE_JWT_SECRET", "")
ONLYOFFICE_DOCS_EXTERNAL_URL = os.getenv("ONLYOFFICE_DOCS_EXTERNAL_URL", "")


def document_key(doc_id: str, revision: int) -> str:
    """Document key tied to content revision so refreshFile fetches the latest xlsx."""
    rev = max(0, int(revision))
    return hashlib.sha256(f"{doc_id}:{rev}".encode()).hexdigest()[:20]


def build_editor_config(
    doc_id: str,
    title: str,
    user_email: str,
    file_url: str,
    callback_url: str,
    revision: int = 0,
) -> dict:
    config = {
        "document": {
            "fileType": "xlsx",
            "key": document_key(doc_id, revision),
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
        # OnlyOffice expects editor config claims at top level (document/editorConfig),
        # not wrapped in a nested "payload" object.
        token = jwt.encode(config, ONLYOFFICE_JWT_SECRET, algorithm="HS256")
        config["token"] = token

    return config


def render_editor_watch_page(*, doc_id: str, frame_url: str, revision: int, poll_ms: int = 3000) -> str:
    """
    Full-screen Nextcloud OnlyOffice iframe that reloads when content_revision changes.

    Mobile/API writes bump revision; the browser polls and refreshes the iframe so
    users see new rows without manually reloading the page.
    """
    safe_url = html.escape(frame_url, quote=True)
    safe_doc_id = html.escape(doc_id, quote=True)
    poll_ms = max(1000, int(poll_ms))
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OnlyOffice</title>
  <style>
    html, body {{
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: #f0f0f0;
    }}
    #oo-frame {{
      border: 0;
      width: 100%;
      height: 100%;
      display: block;
    }}
    #oo-refresh-banner {{
      display: none;
      position: fixed;
      top: 12px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 1000;
      background: #323232;
      color: #fff;
      padding: 8px 14px;
      border-radius: 6px;
      font: 13px/1.4 Arial, sans-serif;
      box-shadow: 0 2px 8px rgba(0,0,0,0.25);
    }}
  </style>
</head>
<body>
  <div id="oo-refresh-banner">Updating spreadsheet…</div>
  <iframe id="oo-frame" data-src="{safe_url}" src="{safe_url}" allow="clipboard-read; clipboard-write"></iframe>
  <script>
    (function () {{
      const docId = "{safe_doc_id}";
      const pollMs = {poll_ms};
      let revision = {int(revision)};
      const frame = document.getElementById("oo-frame");
      const banner = document.getElementById("oo-refresh-banner");
      const baseSrc = frame.dataset.src || frame.src;

      function reloadFrame() {{
        banner.style.display = "block";
        const joiner = baseSrc.indexOf("?") >= 0 ? "&" : "?";
        frame.src = baseSrc + joiner + "_refresh=" + Date.now();
        window.setTimeout(function () {{
          banner.style.display = "none";
        }}, 2500);
      }}

      window.setInterval(async function () {{
        try {{
          const resp = await fetch("/api/docs/" + encodeURIComponent(docId) + "/revision", {{
            credentials: "same-origin",
            cache: "no-store",
          }});
          if (!resp.ok) return;
          const data = await resp.json();
          const next = Number(data.revision || 0);
          if (next > revision) {{
            revision = next;
            reloadFrame();
          }}
        }} catch (err) {{
          /* ignore transient network errors */
        }}
      }}, pollMs);
    }})();
  </script>
</body>
</html>"""


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


def render_editor_embed_page(
    *,
    doc_id: str,
    editor_session: str,
    config: dict,
    revision: int,
    poll_ms: int = 3000,
) -> str:
    """
    Embedded OnlyOffice editor that polls content_revision and calls refreshFile()
    when mobile/API writes new rows — no full page or iframe reload.
    """
    docs_url = html.escape(ONLYOFFICE_DOCS_EXTERNAL_URL.rstrip("/"), quote=True)
    safe_doc_id = html.escape(doc_id, quote=True)
    safe_session = html.escape(editor_session, quote=True)
    config_json = json.dumps(config, ensure_ascii=False)
    poll_ms = max(1000, int(poll_ms))
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OnlyOffice</title>
  <style>
    html, body, #editor {{
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: #f0f0f0;
    }}
    #oo-refresh-banner {{
      display: none;
      position: fixed;
      top: 12px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 1000;
      background: #323232;
      color: #fff;
      padding: 8px 14px;
      border-radius: 6px;
      font: 13px/1.4 Arial, sans-serif;
      box-shadow: 0 2px 8px rgba(0,0,0,0.25);
    }}
  </style>
</head>
<body>
  <div id="oo-refresh-banner">Updating spreadsheet…</div>
  <div id="editor"></div>
  <script src="{docs_url}/web-apps/apps/api/documents/api.js"></script>
  <script>
    (function () {{
      const docId = "{safe_doc_id}";
      const editorSession = "{safe_session}";
      const pollMs = {poll_ms};
      let revision = {int(revision)};
      let docEditor = null;
      const banner = document.getElementById("oo-refresh-banner");
      const initialConfig = {config_json};

      function configUrl(since) {{
        const params = new URLSearchParams({{
          since: String(since),
          editor_session_id: editorSession,
        }});
        return "/api/docs/" + encodeURIComponent(docId) + "/editor-config?" + params.toString();
      }}

      async function applyRefresh(fromUserRequest) {{
        try {{
          const resp = await fetch(configUrl(revision), {{
            credentials: "same-origin",
            cache: "no-store",
          }});
          if (!resp.ok) return;
          const data = await resp.json();
          const next = Number(data.revision || 0);
          const cfg = data.config;
          if (!cfg || !docEditor || typeof docEditor.refreshFile !== "function") {{
            if (fromUserRequest && cfg && docEditor) {{
              docEditor.refreshFile(cfg);
            }}
            return;
          }}
          if (next > revision || fromUserRequest) {{
            revision = Math.max(revision, next);
            banner.style.display = "block";
            docEditor.refreshFile(cfg);
            window.setTimeout(function () {{
              banner.style.display = "none";
            }}, 1500);
          }}
        }} catch (err) {{
          /* ignore transient network errors */
        }}
      }}

      initialConfig.events = {{
        onRequestRefreshFile: function () {{
          applyRefresh(true);
        }},
      }};

      docEditor = new DocsAPI.DocEditor("editor", initialConfig);
      window.setInterval(function () {{
        applyRefresh(false);
      }}, pollMs);
    }})();
  </script>
</body>
</html>"""
