import logging
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

_DELIVERABLE_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)


def is_deliverable_email(address: str) -> bool:
    """True when the value looks like a real mailbox (not a Nextcloud uid hash)."""
    text = (address or "").strip().lower()
    return bool(text and _DELIVERABLE_EMAIL_RE.fullmatch(text))


def user_facing_delivery_error() -> str:
    return (
        "Could not send notification email to the document owner. "
        "Ask the owner to share the spreadsheet with your account email."
    )


def smtp_configured() -> bool:
    return bool(
        (os.getenv("EMAIL_USER") or "").strip()
        and (os.getenv("EMAIL_PASSWORD") or "").strip()
    )


def _smtp_settings() -> tuple[str, int, str, str, str]:
    host = (os.getenv("EMAIL_HOST") or "smtp.gmail.com").strip()
    port_raw = (os.getenv("EMAIL_PORT") or "587").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 587
    user = (os.getenv("EMAIL_USER") or "").strip()
    password = (os.getenv("EMAIL_PASSWORD") or "").strip()
    secure = "ssl" if port == 465 else ("tls" if port != 25 else "")
    return host, port, user, password, secure


def _from_address() -> str:
    user = (os.getenv("EMAIL_USER") or "").strip()
    if "@" in user:
        return user
    domain = (os.getenv("MAIL_FROM_DOMAIN") or "localhost").strip()
    return f"{user or 'noreply'}@{domain}"


def send_access_request_email(
    *,
    owner_email: str,
    requester_email: str,
    doc_title: str,
    review_url: str,
) -> None:
    if not smtp_configured():
        raise RuntimeError("SMTP is not configured (EMAIL_USER / EMAIL_PASSWORD)")

    recipient = owner_email.strip().lower()
    if not is_deliverable_email(recipient):
        raise ValueError("Document owner does not have a deliverable email address")

    host, port, user, password, secure = _smtp_settings()
    subject = f"Access request: {doc_title}"
    text_body = (
        f"{requester_email} is requesting edit access to the spreadsheet "
        f"\"{doc_title}\".\n\n"
        f"Open this page to grant or deny access (sign in as the document owner):\n"
        f"{review_url}\n"
    )
    html_body = f"""
    <html><body style="font-family: sans-serif; line-height: 1.5;">
      <p><strong>{requester_email}</strong> is requesting <strong>edit</strong> access to
      <strong>{doc_title}</strong>.</p>
      <p>
        <a href="{review_url}" style="display:inline-block;padding:10px 16px;background:#007bff;color:#fff;text-decoration:none;border-radius:4px;">
          Review request
        </a>
      </p>
      <p style="color:#666;font-size:14px;">You must sign in as the document owner to grant or deny access.</p>
    </body></html>
    """

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = _from_address()
    message["To"] = recipient
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if secure == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            server.ehlo()
            if secure == "tls":
                server.starttls()
                server.ehlo()
            server.login(user, password)
            server.sendmail(message["From"], [message["To"]], message.as_string())
    except Exception as exc:
        logger.exception("Failed to send access request email to %s", recipient)
        raise RuntimeError(user_facing_delivery_error()) from exc
