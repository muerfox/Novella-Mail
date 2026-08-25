"""
IMAP/SMTP service layer. The panel has no mail data of its own - it's a thin
client against the real mailbox on the mail server (same as Thunderbird or
any other IMAP/SMTP client would be), so every read/write here is a live
protocol call.
"""
import smtplib
import ssl
from email import message_from_bytes, policy
from email.header import decode_header
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

import bleach
from django.conf import settings
from imapclient import IMAPClient

_ALLOWED_TAGS = [
    "a", "b", "i", "u", "em", "strong", "p", "div", "span", "br", "hr",
    "ul", "ol", "li", "blockquote", "pre", "code",
    "table", "thead", "tbody", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "img", "font",
]
_ALLOWED_ATTRS = {
    "a": ["href", "title", "target"],
    "img": ["src", "alt", "width", "height"],
    "font": ["color", "size", "face"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto", "cid"]

_SPECIAL_USE_FLAGS = {
    "sent": (b"\\Sent",),
    "trash": (b"\\Trash",),
    "drafts": (b"\\Drafts",),
    "junk": (b"\\Junk",),
}
_FALLBACK_NAMES = {
    "sent": ["Sent", "Sent Items", "Sent Messages", "INBOX.Sent"],
    "trash": ["Trash", "Deleted Items", "INBOX.Trash"],
    "drafts": ["Drafts", "INBOX.Drafts"],
    "junk": ["Junk", "Spam", "INBOX.Junk"],
}


class MailError(Exception):
    """Raised for any IMAP/SMTP failure the view layer should show to the user."""


def _ssl_context():
    ctx = ssl.create_default_context()
    if settings.MAIL_TLS_INSECURE:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _decode_words(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    out = []
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _format_address(addr):
    if addr is None:
        return ""
    name = _decode_words(addr.name) if addr.name else ""
    mailbox = addr.mailbox.decode("utf-8", errors="replace") if addr.mailbox else ""
    host = addr.host.decode("utf-8", errors="replace") if addr.host else ""
    email_addr = f"{mailbox}@{host}" if mailbox and host else mailbox
    return f"{name} <{email_addr}>" if name else email_addr


def _format_addresses(addrs):
    if not addrs:
        return ""
    return ", ".join(_format_address(a) for a in addrs)


def sanitize_html(html):
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )


# --- Connection -------------------------------------------------------

def get_imap_connection(email_addr, password):
    if not settings.MAIL_IMAP_HOST:
        raise MailError("MAIL_IMAP_HOST is not configured.")
    try:
        server = IMAPClient(
            settings.MAIL_IMAP_HOST,
            port=settings.MAIL_IMAP_PORT,
            ssl=True,
            ssl_context=_ssl_context(),
            timeout=20,
        )
        server.login(email_addr, password)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as one message
        raise MailError(f"Could not log in: {exc}") from exc
    return server


# --- Folders ------------------------------------------------------------

def list_folders(server):
    try:
        raw = server.list_folders()
    except Exception as exc:  # noqa: BLE001
        raise MailError(f"Could not list folders: {exc}") from exc
    folders = []
    for flags, _delimiter, name in raw:
        if b"\\Noselect" in flags:
            continue
        folders.append({"name": name, "flags": flags})
    folders.sort(key=lambda f: (f["name"] != "INBOX", f["name"].lower()))
    return folders


def find_special_folder(folders, kind):
    special_flags = _SPECIAL_USE_FLAGS.get(kind, ())
    for f in folders:
        if any(flag in f["flags"] for flag in special_flags):
            return f["name"]
    names = {f["name"] for f in folders}
    for candidate in _FALLBACK_NAMES.get(kind, []):
        if candidate in names:
            return candidate
    return None


# --- Listing / reading messages -----------------------------------------

def list_messages(server, folder, page=1, per_page=None):
    per_page = per_page or settings.MESSAGES_PER_PAGE
    try:
        server.select_folder(folder, readonly=True)
        uids = server.search(["ALL"])
        uids.sort(reverse=True)
        total = len(uids)
        start = (page - 1) * per_page
        page_uids = uids[start:start + per_page]
        resp = server.fetch(page_uids, ["ENVELOPE", "FLAGS"]) if page_uids else {}
    except Exception as exc:  # noqa: BLE001
        raise MailError(f"Could not list messages: {exc}") from exc

    messages = []
    for uid in page_uids:
        data = resp.get(uid, {})
        env = data.get(b"ENVELOPE")
        flags = data.get(b"FLAGS", ())
        messages.append({
            "uid": uid,
            "subject": _decode_words(env.subject) if env and env.subject else "(no subject)",
            "from": _format_addresses(env.from_) if env else "",
            "date": env.date if env else None,
            "seen": b"\\Seen" in flags,
            "flagged": b"\\Flagged" in flags,
        })
    return messages, total


def _parse_message(msg, uid, raw_bytes):
    plain_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    text_body = plain_part.get_content() if plain_part else None
    html_body_raw = html_part.get_content() if html_part else None
    html_body = sanitize_html(html_body_raw) if html_body_raw else None

    attachments = []
    for idx, part in enumerate(msg.iter_attachments()):
        payload = part.get_payload(decode=True) or b""
        attachments.append({
            "index": idx,
            "filename": part.get_filename() or f"attachment-{idx}",
            "content_type": part.get_content_type(),
            "size": len(payload),
        })

    return {
        "uid": uid,
        "subject": str(msg.get("Subject", "(no subject)")),
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "cc": str(msg.get("Cc", "")),
        "date": str(msg.get("Date", "")),
        "message_id": str(msg.get("Message-ID", "")),
        "text_body": text_body,
        "html_body": html_body,
        "attachments": attachments,
        "raw": raw_bytes,
    }


def get_message(server, folder, uid, mark_seen=True):
    try:
        server.select_folder(folder, readonly=not mark_seen)
        resp = server.fetch([uid], ["RFC822"])
    except Exception as exc:  # noqa: BLE001
        raise MailError(f"Could not fetch message: {exc}") from exc
    if uid not in resp:
        return None
    raw = resp[uid][b"RFC822"]
    msg = message_from_bytes(raw, policy=policy.default)
    if mark_seen:
        try:
            server.add_flags([uid], [b"\\Seen"])
        except Exception:  # noqa: BLE001 - non-fatal, message is still readable
            pass
    return _parse_message(msg, uid, raw)


def get_attachment(server, folder, uid, index):
    server.select_folder(folder, readonly=True)
    resp = server.fetch([uid], ["RFC822"])
    if uid not in resp:
        return None
    msg = message_from_bytes(resp[uid][b"RFC822"], policy=policy.default)
    for idx, part in enumerate(msg.iter_attachments()):
        if idx == index:
            return {
                "filename": part.get_filename() or f"attachment-{idx}",
                "content_type": part.get_content_type(),
                "content": part.get_payload(decode=True) or b"",
            }
    return None


# --- Mutations ------------------------------------------------------------

def set_flag(server, folder, uid, flag, value=True):
    server.select_folder(folder, readonly=False)
    if value:
        server.add_flags([uid], [flag])
    else:
        server.remove_flags([uid], [flag])


def move_message(server, folder, uid, target_folder):
    server.select_folder(folder, readonly=False)
    server.move([uid], target_folder)


def delete_message(server, folder, uid):
    server.select_folder(folder, readonly=False)
    server.add_flags([uid], [b"\\Deleted"])
    server.expunge()


# --- Sending ----------------------------------------------------------

def build_message(from_addr, to, cc, subject, body_text, in_reply_to=None, references=None, attachments=None):
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body_text or "")
    for att in attachments or []:
        content_type = att.get("content_type") or "application/octet-stream"
        maintype, _, subtype = content_type.partition("/")
        msg.add_attachment(
            att["content"],
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att["filename"],
        )
    recipients = [addr for _, addr in getaddresses([to or "", cc or ""]) if addr]
    return msg, recipients


def send_message(email_addr, password, msg, recipients):
    if not settings.MAIL_SMTP_HOST:
        raise MailError("MAIL_SMTP_HOST is not configured.")
    if not recipients:
        raise MailError("No recipients specified.")
    try:
        with smtplib.SMTP(settings.MAIL_SMTP_HOST, settings.MAIL_SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=_ssl_context())
            smtp.ehlo()
            smtp.login(email_addr, password)
            smtp.send_message(msg, to_addrs=recipients)
    except Exception as exc:  # noqa: BLE001
        raise MailError(f"Could not send message: {exc}") from exc


def append_to_sent(server, folders, msg):
    sent_folder = find_special_folder(folders, "sent")
    if not sent_folder:
        return
    try:
        server.append(sent_folder, msg.as_bytes(), flags=[b"\\Seen"])
    except Exception:  # noqa: BLE001 - the send already succeeded, don't fail the request
        pass
