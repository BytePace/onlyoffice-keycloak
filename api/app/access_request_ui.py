from html import escape


def _layout(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f5f7fb; color: #1f2937; }}
    .wrap {{ max-width: 640px; margin: 40px auto; padding: 0 16px; }}
    .card {{ background: #fff; border-radius: 10px; padding: 24px; box-shadow: 0 8px 24px rgba(15,23,42,.08); }}
    h1 {{ margin-top: 0; font-size: 1.5rem; }}
    p {{ line-height: 1.5; }}
    .meta {{ background: #f8fafc; border-radius: 8px; padding: 12px 14px; margin: 16px 0; }}
    .actions {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 20px; }}
    button, .btn {{
      display: inline-block; border: none; border-radius: 8px; padding: 12px 18px;
      font-size: 1rem; cursor: pointer; text-decoration: none;
    }}
    .btn-primary {{ background: #2563eb; color: #fff; }}
    .btn-danger {{ background: #dc2626; color: #fff; }}
    .btn-secondary {{ background: #e5e7eb; color: #111827; }}
    .status {{ font-weight: 600; }}
    .ok {{ color: #15803d; }}
    .warn {{ color: #b45309; }}
    .err {{ color: #b91c1c; }}
  </style>
</head>
<body>
  <div class="wrap"><div class="card">{body}</div></div>
</body>
</html>"""


def review_page(
    *,
    record: dict,
    mode: str,
    logged_in_email: str | None = None,
    login_url: str = "",
    grant_action: str = "",
    deny_action: str = "",
    error: str = "",
) -> str:
    title = "Spreadsheet access request"
    requester = escape(record.get("requester_email") or "")
    doc_title = escape(record.get("doc_title") or record.get("doc_id") or "Spreadsheet")
    owner = escape(record.get("owner_email") or "")
    status = record.get("status") or "pending"
    role = escape(record.get("requested_role") or "editor")

    if status != "pending":
        status_html = f'<p class="status ok">This request was already <strong>{escape(status)}</strong>.</p>'
        return _layout(title, f"<h1>{title}</h1>{status_html}")

    meta_block = f"""
    <div class="meta">
      <div><strong>Document:</strong> {doc_title}</div>
      <div><strong>Requested by:</strong> {requester}</div>
      <div><strong>Access level:</strong> {role}</div>
      <div><strong>Owner:</strong> {owner}</div>
    </div>
    """
    error_html = f'<p class="status err">{escape(error)}</p>' if error else ""

    if mode == "login":
        body = f"""
        <h1>{title}</h1>
        {meta_block}
        <p>Sign in as the document owner to grant or deny this request.</p>
        <div class="actions">
          <a class="btn btn-primary" href="{escape(login_url)}">Sign in to respond</a>
        </div>
        """
        return _layout(title, body)

    if mode == "readonly":
        body = f"<h1>{title}</h1>{error_html}{meta_block}"
        return _layout(title, body)

    logged_in = escape(logged_in_email or "")
    body = f"""
    <h1>{title}</h1>
    {error_html}
    {meta_block}
    <div><strong>Signed in as:</strong> {logged_in}</div>
    <p>Choose whether to grant edit access to this spreadsheet.</p>
    <div class="actions">
      <form method="post" action="{escape(grant_action)}">
        <button class="btn-primary" type="submit">Grant edit access</button>
      </form>
      <form method="post" action="{escape(deny_action)}">
        <button class="btn-danger" type="submit">Deny</button>
      </form>
    </div>
    """
    return _layout(title, body)


def result_page(*, title: str, message: str, tone: str = "ok") -> str:
    return _layout(
        title,
        f'<h1>{escape(title)}</h1><p class="status {tone}">{message}</p>',
    )
