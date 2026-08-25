"""
IMAP/SMTP service layer. The panel has no mail data of its own - it's a thin
client against the real mailbox on the mail server (same as Thunderbird or
any other IMAP/SMTP client would be), so every read/write here is a live
protocol call.

Every function that talks to the server takes an already-connected
IMAPClient (`server`, from get_imap_connection below) rather than opening
its own connection - the caller (webmail/views.py) is responsible for
connecting once per request and logging out when it's done, so a single
request can list folders, fetch a message, and mark it read without paying
for three separate TCP+TLS handshakes.
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

# Allowed HTML tags/attributes when rendering an incoming HTML email body.
# This is a strict allow-list (not a block-list) on purpose: email is
# attacker-controlled content, so anything not explicitly listed here -
# <script>, <iframe>, event handler attributes like onclick, javascript:
# URLs, etc. - gets stripped by bleach.clean() below, regardless of how
# creatively it's encoded. The sanitized result is *also* rendered inside a
# sandboxed <iframe> in the template (see templates/webmail/
# _message_detail.html) as a second, independent layer of defense.
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

# IMAP servers that support the SPECIAL-USE extension (RFC 6154) tag
# folders like Sent/Trash/Drafts/Junk with these flags, so we don't have to
# guess the folder name. Not every server/account advertises them, though,
# so _FALLBACK_NAMES below covers the common plain-English folder names as
# a second attempt.
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
    """
    Build the TLS context used for both IMAP and SMTP connections.

    MAIL_TLS_INSECURE (see novella/settings.py) disables certificate
    verification entirely - needed by default because docker-mailserver's
    out-of-the-box certificate is self-signed (see .env.example's
    SSL_TYPE). Once you configure a real certificate, set that to false so
    connections actually verify the server's identity again.
    """
    ctx = ssl.create_default_context()
    if settings.MAIL_TLS_INSECURE:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _decode_words(value):
    """
    Decode a raw (possibly RFC 2047 "=?charset?...?=" encoded) email header
    value into a plain Python string. Needed because IMAP ENVELOPE data
    comes back as raw bytes with encoded-word sequences still in them (e.g.
    a Subject with non-ASCII characters), unlike the `email` package's own
    header objects (used in _parse_message below) which decode this for us
    automatically.
    """
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
    """Turn one IMAPClient Address (from an ENVELOPE) into "Name <user@host>"."""
    if addr is None:
        return ""
    name = _decode_words(addr.name) if addr.name else ""
    mailbox = addr.mailbox.decode("utf-8", errors="replace") if addr.mailbox else ""
    host = addr.host.decode("utf-8", errors="replace") if addr.host else ""
    email_addr = f"{mailbox}@{host}" if mailbox and host else mailbox
    return f"{name} <{email_addr}>" if name else email_addr


def _format_addresses(addrs):
    """Format a tuple of ENVELOPE addresses (e.g. env.from_) as a comma-separated string."""
    if not addrs:
        return ""
    return ", ".join(_format_address(a) for a in addrs)


def sanitize_html(html):
    """Strip an HTML email body down to the safe tag/attribute allow-list above."""
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )


# --- Connection -------------------------------------------------------

def get_imap_connection(email_addr, password):
    """
    Open a fresh IMAP connection and log in as the given mailbox.

    This doubles as the panel's *authentication* mechanism: there's no
    separate user database, so "is this email+password correct" is
    answered by literally attempting this IMAP login (see the login view
    in webmail/views.py) and seeing whether it succeeds.
    """
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
    """
    List selectable folders (mailboxes), INBOX first then alphabetical.

    \\Noselect folders (pure "directory" nodes some servers expose purely
    for organizing other folders, not actual mailboxes you can open) are
    skipped since there's nothing to show for them.
    """
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
    """
    Find the Sent/Trash/Drafts/Junk folder from a list_folders() result.

    Tries the standard SPECIAL-USE flag first, then falls back to common
    plain-English names, since not every account advertises SPECIAL-USE.
    Returns None if nothing matches (caller decides what to do then - e.g.
    message_action in views.py falls back to permanently deleting instead
    of moving to Trash if no Trash folder can be found).
    """
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
    """
    Fetch one page of messages in `folder`, newest first.

    Only fetches ENVELOPE (headers) and FLAGS for the page being displayed
    - not the full message body - so listing a folder with thousands of
    messages stays fast; the full body is only fetched when you actually
    open one message (see get_message below).
    """
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
    """
    Turn a parsed `email.message.EmailMessage` into the plain dict the
    templates use. Prefers the plain-text body for `text_body` and the
    (sanitized) HTML body for `html_body` - the template shows whichever
    is available, preferring HTML if both exist (see
    templates/webmail/_message_detail.html).
    """
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
    """
    Fetch and parse one full message by UID.

    Set mark_seen=False when peeking at a message without the user
    explicitly opening it (e.g. quoting the original when composing a
    reply, in compose_view) - selecting the folder read-only in that case
    avoids marking it read as a side effect of building the reply form.
    """
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
    """
    Fetch one attachment's raw bytes by its position in the message.

    Re-fetches and re-parses the whole message rather than caching
    anything between requests (the panel is stateless between requests by
    design) - `index` has to match the same iter_attachments() ordering
    _parse_message used when it first listed this message's attachments.
    """
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
    """Add (value=True) or remove (value=False) an IMAP flag, e.g. b"\\Seen" or b"\\Flagged"."""
    server.select_folder(folder, readonly=False)
    if value:
        server.add_flags([uid], [flag])
    else:
        server.remove_flags([uid], [flag])


def move_message(server, folder, uid, target_folder):
    """Move a message to another folder (uses the IMAP MOVE extension)."""
    server.select_folder(folder, readonly=False)
    server.move([uid], target_folder)


def delete_message(server, folder, uid):
    """Permanently delete a message (flag \\Deleted, then EXPUNGE). No undo."""
    server.select_folder(folder, readonly=False)
    server.add_flags([uid], [b"\\Deleted"])
    server.expunge()


# --- Sending ----------------------------------------------------------

def build_message(from_addr, to, cc, subject, body_text, in_reply_to=None, references=None, attachments=None):
    """
    Build a MIME message ready to hand to send_message() below.

    Also returns the flat list of actual recipient addresses (To + Cc,
    parsed out of whatever free-text the user typed, e.g. "a@x.com,
    b@y.com") for the SMTP envelope - that's a separate thing from the
    human-readable To/Cc *headers* set on the message itself.
    """
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
    """
    Send a built message via SMTP submission (STARTTLS on MAIL_SMTP_PORT,
    587 by default) authenticated as the given mailbox. Note this does
    *not* also save a copy to the Sent folder - see append_to_sent below,
    called separately by the caller after a successful send.
    """
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
    """
    Save a copy of a just-sent message into the account's Sent folder.

    SMTP itself has no concept of "Sent" - a mail server just relays the
    message onward, it doesn't keep a copy for the sender. Desktop/webmail
    clients handle this themselves by uploading a copy via IMAP APPEND
    after a successful send, which is exactly what this does. Silently
    does nothing if no Sent folder can be found, and silently ignores a
    failed append - by the time this runs the message has already been
    sent successfully, so a Sent-folder hiccup shouldn't be reported to
    the user as a failure to send.
    """
    sent_folder = find_special_folder(folders, "sent")
    if not sent_folder:
        return
    try:
        server.append(sent_folder, msg.as_bytes(), flags=[b"\\Seen"])
    except Exception:  # noqa: BLE001 - the send already succeeded, don't fail the request
        pass
