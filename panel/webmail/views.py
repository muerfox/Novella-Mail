"""
All the panel's views (Django's term for "the function that handles a URL
and returns a response" - see urls.py for which URL maps to which of these).

Two halves to this file:
  - Mail views (login/logout/inbox/compose/attachments): every logged-in
    user can use these for their own mailbox. They work by opening a fresh
    IMAP connection per request via webmail.mail (see that module's own
    docstring for why) using the email+password stashed in the session at
    login time.
  - Domain admin views (bottom, "Domain admin" section): gated behind
    @admin_required (see decorators.py) - only mailbox emails listed in
    ADMIN_EMAILS can reach these. They call webmail.mailadmin, which edits
    docker-mailserver's config files directly rather than making any
    network call.
"""
import math
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from . import crypto, mail, mailadmin
from .decorators import admin_required, mail_login_required
from .forms import (
    AddDomainForm,
    AddMailboxForm,
    ChangePasswordForm,
    ClientForm,
    ComposeForm,
    LoginForm,
    QuotaForm,
)


def login_view(request):
    """
    Log in with a real mailbox email + password.

    There's no local user database - "is this correct" is answered by
    actually attempting an IMAP login (get_imap_connection). On success we
    keep the email in the session in plain text and the password encrypted
    (see crypto.py) so later requests can re-authenticate to IMAP/SMTP
    without asking the user to type their password on every page.
    """
    if request.session.get("mail_email"):
        return redirect("webmail:mail")

    error = None
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email_addr = form.cleaned_data["email"]
        password = form.cleaned_data["password"]
        try:
            server = mail.get_imap_connection(email_addr, password)
            server.logout()
        except mail.MailError as exc:
            error = str(exc)
            if mailadmin.is_mailbox_suspended(email_addr):
                error = "This mailbox is suspended. Please contact your email provider."
        else:
            request.session["mail_email"] = email_addr
            request.session["mail_password_enc"] = crypto.encrypt(password)
            request.session.set_expiry(0)  # expire when the browser closes
            return redirect("webmail:mail")

    return render(request, "webmail/login.html", {"form": form, "error": error})


def logout_view(request):
    """Clear the session (email + encrypted password) and send back to login."""
    request.session.flush()
    return redirect("webmail:login")


def _get_session_creds(request):
    """Read back (email, password) from the session, decrypting the password."""
    email_addr = request.session.get("mail_email")
    enc = request.session.get("mail_password_enc")
    password = crypto.decrypt(enc) if enc else None
    return email_addr, password


def _connect_or_bail(request):
    """
    Shared "get me a live IMAP connection or send the user back to login"
    helper used by every mail view below. If the stored credentials are
    missing or the mail server rejects them (e.g. the password was changed
    elsewhere since login), the session is flushed and the caller should
    immediately return the redirect it gets back.

    Returns (server, None) on success, or (None, redirect_response) on failure.
    """
    email_addr, password = _get_session_creds(request)
    if not email_addr or not password:
        return None, redirect("webmail:login")
    try:
        server = mail.get_imap_connection(email_addr, password)
    except mail.MailError:
        request.session.flush()
        messages.error(request, "Your mail session expired or credentials were rejected. Please log in again.")
        return None, redirect("webmail:login")
    return server, None


def _safe_int(value, default):
    """int(value), or `default` if value is missing/not a valid integer."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@mail_login_required
def mail_view(request):
    """
    The main inbox page: folder list + message list + (optionally) one
    open message, all in a single response. Which folder/page/message to
    show comes from query params (?folder=...&page=...&uid=...) rather
    than separate URLs, which keeps every "click a folder" / "click a
    message" / "next page" link a plain GET to this same view - see
    templates/webmail/mail.html for how the three panes lay out
    responsively from this one context.
    """
    server, bail = _connect_or_bail(request)
    if bail:
        return bail

    folder = request.GET.get("folder") or "INBOX"
    page = max(_safe_int(request.GET.get("page"), 1), 1)
    uid = request.GET.get("uid")

    folders, msg_list, total, detail, quota = [], [], 0, None, None
    try:
        folders = mail.list_folders(server)
        quota = mail.get_quota(server)
        msg_list, total = mail.list_messages(server, folder, page=page)
        if uid:
            uid_int = _safe_int(uid, None)
            if uid_int is not None:
                detail = mail.get_message(server, folder, uid_int)
    except mail.MailError as exc:
        messages.error(request, str(exc))
    finally:
        # Always log out of the IMAP connection before this view returns -
        # there's no long-lived connection pool, each request opens and
        # closes its own (see webmail/mail.py's module docstring).
        try:
            server.logout()
        except Exception:  # noqa: BLE001
            pass

    total_pages = max(math.ceil(total / settings.MESSAGES_PER_PAGE), 1) if total else 1

    context = {
        "folders": folders,
        "current_folder": folder,
        "messages_list": msg_list,
        "detail": detail,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "selected_uid": _safe_int(uid, None),
        "quota": quota,
    }
    return render(request, "webmail/mail.html", context)


@mail_login_required
@require_http_methods(["POST"])
def message_action(request):
    """
    Handle the small per-message actions (delete/mark read/unread/star) -
    each one is its own tiny POST form in the message list/detail
    templates. Deleting moves to Trash if a Trash folder can be found,
    otherwise deletes permanently. Redirects back to the same folder/page
    (and message, unless it was just deleted) rather than rendering
    directly, so a page refresh after the action doesn't re-submit the form.
    """
    folder = request.POST.get("folder") or "INBOX"
    uid = request.POST.get("uid")
    action = request.POST.get("action")
    page = request.POST.get("page", "1")

    server, bail = _connect_or_bail(request)
    if bail:
        return bail

    uid_int = _safe_int(uid, None)
    try:
        if uid_int is None:
            raise mail.MailError("Missing message id.")
        elif action == "delete":
            folders = mail.list_folders(server)
            trash = mail.find_special_folder(folders, "trash")
            if trash and trash != folder:
                mail.move_message(server, folder, uid_int, trash)
            else:
                mail.delete_message(server, folder, uid_int)
            messages.success(request, "Message deleted.")
        elif action == "mark_read":
            mail.set_flag(server, folder, uid_int, b"\\Seen", True)
        elif action == "mark_unread":
            mail.set_flag(server, folder, uid_int, b"\\Seen", False)
        elif action == "flag":
            mail.set_flag(server, folder, uid_int, b"\\Flagged", True)
        elif action == "unflag":
            mail.set_flag(server, folder, uid_int, b"\\Flagged", False)
        else:
            messages.error(request, "Unknown action.")
    except mail.MailError as exc:
        messages.error(request, f"Action failed: {exc}")
    finally:
        try:
            server.logout()
        except Exception:  # noqa: BLE001
            pass

    query = {"folder": folder, "page": page}
    if action != "delete" and uid_int is not None:
        query["uid"] = uid_int
    return redirect(f"{reverse('webmail:mail')}?{urlencode(query)}")


@mail_login_required
def compose_view(request):
    """
    Compose a new message, or a reply/reply-all/forward of an existing one.

    GET with ?reply=<uid>&folder=<folder>&mode=reply|reply-all|forward
    pre-fills the form (quoting the original) - used by the Reply/Reply
    all/Forward links on the message detail page. Plain GET with no
    `reply` param is a blank compose form.

    POST sends the message, then appends a copy to the Sent folder - see
    webmail.mail.send_message and append_to_sent for why those are two
    separate steps (SMTP has no concept of "sent mail" on its own).
    """
    email_addr, password = _get_session_creds(request)
    initial = {}

    if request.method == "POST":
        form = ComposeForm(request.POST)
        if form.is_valid():
            attachments = [
                {"filename": f.name, "content_type": f.content_type, "content": f.read()}
                for f in request.FILES.getlist("attachments")
            ]
            msg, recipients = mail.build_message(
                from_addr=email_addr,
                to=form.cleaned_data["to"],
                cc=form.cleaned_data["cc"],
                subject=form.cleaned_data["subject"] or "(no subject)",
                body_text=form.cleaned_data["body"],
                in_reply_to=form.cleaned_data["in_reply_to"] or None,
                references=form.cleaned_data["references"] or None,
                attachments=attachments,
            )
            try:
                mail.send_message(email_addr, password, msg, recipients)
            except mail.MailError as exc:
                messages.error(request, str(exc))
            else:
                # Sending succeeded - now open a *second* connection just to
                # save a copy into Sent. Doing this even if it fails
                # wouldn't un-send the message, so failures here are
                # swallowed inside append_to_sent rather than shown to the
                # user as a "could not send" error.
                server, _bail = _connect_or_bail(request)
                if server:
                    try:
                        folders = mail.list_folders(server)
                        mail.append_to_sent(server, folders, msg)
                    finally:
                        try:
                            server.logout()
                        except Exception:  # noqa: BLE001
                            pass
                messages.success(request, "Message sent.")
                return redirect("webmail:mail")
    else:
        reply_uid = _safe_int(request.GET.get("reply"), None)
        reply_folder = request.GET.get("folder")
        mode = request.GET.get("mode", "reply")

        if reply_uid is not None and reply_folder:
            # Fetch the original message read-only (mark_seen=False) just
            # to quote it - opening the compose form for a reply shouldn't
            # have the side effect of marking the original message read.
            server, _bail = _connect_or_bail(request)
            original = None
            if server:
                try:
                    original = mail.get_message(server, reply_folder, reply_uid, mark_seen=False)
                finally:
                    try:
                        server.logout()
                    except Exception:  # noqa: BLE001
                        pass
            if original:
                quoted = "\n".join(f"> {line}" for line in (original["text_body"] or "").splitlines())
                if mode == "forward":
                    initial = {
                        "subject": f"Fwd: {original['subject']}",
                        "body": (
                            "\n\n---------- Forwarded message ----------\n"
                            f"From: {original['from']}\nDate: {original['date']}\n"
                            f"Subject: {original['subject']}\n\n{original['text_body'] or ''}"
                        ),
                    }
                else:
                    subject = original["subject"]
                    if not subject.lower().startswith("re:"):
                        subject = f"Re: {subject}"
                    initial = {
                        "to": original["from"],
                        "cc": original["to"] if mode == "reply-all" else "",
                        "subject": subject,
                        "body": f"\n\nOn {original['date']}, {original['from']} wrote:\n{quoted}",
                        "in_reply_to": original["message_id"],
                        "references": original["message_id"],
                    }
        form = ComposeForm(initial=initial)

    return render(request, "webmail/compose.html", {"form": form})


@mail_login_required
def change_password_view(request):
    """
    Let the logged-in mailbox owner change their own password. On success the
    session's encrypted password is updated so IMAP/SMTP keep working without
    forcing a re-login. docker-mailserver picks up the new hash within a few
    seconds; until then a freshly opened mail client may still see the old one.
    """
    email_addr, _password = _get_session_creds(request)
    form = ChangePasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            mailadmin.change_mailbox_password(
                email_addr,
                form.cleaned_data["current_password"],
                form.cleaned_data["new_password"],
            )
        except mailadmin.MailAdminError as exc:
            messages.error(request, str(exc))
        else:
            request.session["mail_password_enc"] = crypto.encrypt(form.cleaned_data["new_password"])
            messages.success(request, "Password changed. It takes effect within a few seconds.")
            return redirect("webmail:mail")

    return render(request, "webmail/change_password.html", {"form": form})


@mail_login_required
def attachment_download(request):
    """Stream one attachment's bytes back with a Content-Disposition header, so the browser downloads/saves it."""
    folder = request.GET.get("folder")
    uid = _safe_int(request.GET.get("uid"), None)
    index = _safe_int(request.GET.get("index"), None)
    if not folder or uid is None or index is None:
        return HttpResponseBadRequest("Missing folder/uid/index.")

    server, bail = _connect_or_bail(request)
    if bail:
        return bail
    try:
        att = mail.get_attachment(server, folder, uid, index)
    finally:
        try:
            server.logout()
        except Exception:  # noqa: BLE001
            pass

    if not att:
        raise Http404("Attachment not found.")

    response = HttpResponse(att["content"], content_type=att["content_type"] or "application/octet-stream")
    # Strip quotes from the filename so it can't break out of the
    # Content-Disposition header's own quoted-string syntax.
    filename = att["filename"].replace('"', "")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# --- Domain admin (file-based - see webmail.mailadmin) -------------------
# Everything below is gated by @admin_required: only mailbox emails listed
# in the ADMIN_EMAILS setting can reach these views. Unlike the mail views
# above, these don't open an IMAP connection at all - webmail.mailadmin
# manages docker-mailserver purely by reading/writing its config files on
# a volume shared between the two containers.

def _clean_domain(value):
    """
    Validate and lowercase a domain taken from a URL or POST field. These
    end up in filenames and Rspamd's shared dkim_signing.conf, so anything
    AddDomainForm wouldn't accept must never reach webmail.mailadmin.
    """
    form = AddDomainForm({"domain": value or ""})
    if not form.is_valid():
        raise Http404("Invalid domain.")
    return form.cleaned_data["domain"]


@admin_required
def domain_list(request):
    """List known domains, and handle the "add a domain" form."""
    form = AddDomainForm()
    if request.method == "POST":
        form = AddDomainForm(request.POST)
        if form.is_valid():
            # No create step - docker-mailserver derives domains from
            # whatever addresses exist. Just go to that domain's page.
            return redirect("webmail:admin_domain_detail", domain=form.cleaned_data["domain"])

    return render(request, "webmail/admin_domains.html", {"domains": mailadmin.list_domains(), "form": form})


@admin_required
@require_http_methods(["POST"])
def domain_action(request):
    """Currently just handles domain deletion (its own mailboxes + DKIM key registration go with it)."""
    domain = _clean_domain(request.POST.get("domain"))
    action = request.POST.get("action")
    if action == "delete":
        mailadmin.delete_domain(domain)
        messages.success(request, f"Domain {domain} deleted.")
    return redirect("webmail:admin_domains")


@admin_required
def domain_detail(request, domain):
    """
    One domain's admin page: DNS checklist, DKIM key generation, mailbox
    list + add-mailbox form. `domain` doesn't have to already "exist" in
    any registered sense (there's no such thing - see AddDomainForm) - this
    page works fine for a domain with zero mailboxes and no DKIM key yet,
    that's exactly the state right after typing a brand new domain in.
    """
    clean = _clean_domain(domain)
    if clean != domain:
        return redirect("webmail:admin_domain_detail", domain=clean)
    mailbox_form = AddMailboxForm()

    if request.method == "POST" and request.POST.get("form_name") == "mailbox":
        mailbox_form = AddMailboxForm(request.POST)
        if mailbox_form.is_valid():
            try:
                mailadmin.add_mailbox(
                    domain,
                    mailbox_form.cleaned_data["local_part"],
                    mailbox_form.cleaned_data["password"],
                    quota=mailbox_form.cleaned_data["quota"],
                )
            except mailadmin.MailAdminError as exc:
                messages.error(request, f"Could not add mailbox: {exc}")
            else:
                messages.success(request, "Mailbox created. It's live within a few seconds.")
                return redirect("webmail:admin_domain_detail", domain=domain)

    elif request.method == "POST" and request.POST.get("form_name") == "dkim":
        try:
            mailadmin.generate_dkim(domain)
            messages.success(request, "DKIM key generated - add the DNS record below.")
        except mailadmin.MailAdminError as exc:
            messages.error(request, f"Could not generate DKIM key: {exc}")
        return redirect("webmail:admin_domain_detail", domain=domain)

    elif request.method == "POST" and request.POST.get("form_name") == "owner":
        try:
            mailadmin.assign_domain(request.POST.get("client", ""), domain)
            messages.success(request, "Domain assigned.")
        except mailadmin.MailAdminError as exc:
            messages.error(request, f"Could not assign domain: {exc}")
        return redirect("webmail:admin_domain_detail", domain=domain)

    owner = mailadmin.client_for_domain(domain)
    context = {
        "domain": domain,
        "info": mailadmin.get_domain(domain),
        "mailboxes": mailadmin.list_mailboxes(domain),
        "dkim": mailadmin.get_dkim(domain),
        "mailbox_form": mailbox_form,
        "mail_domain": settings.MAIL_DOMAIN,
        "owner": owner,
        "clients": [] if owner else mailadmin.list_clients(with_usage=False),
    }
    return render(request, "webmail/admin_domain_detail.html", context)


@admin_required
@require_http_methods(["POST"])
def mailbox_action(request):
    """Per-mailbox admin actions from a domain's mailbox table: delete, suspend/unsuspend, change quota."""
    domain = _clean_domain(request.POST.get("domain"))
    username = (request.POST.get("username") or "").strip().lower()
    action = request.POST.get("action")
    if not username.endswith(f"@{domain}"):
        return HttpResponseBadRequest("Mailbox doesn't belong to this domain.")

    try:
        if action == "delete":
            mailadmin.delete_mailbox(username)
            messages.success(request, f"Mailbox {username} deleted.")
        elif action in ("suspend", "unsuspend"):
            mailadmin.set_mailbox_suspended(username, action == "suspend")
            messages.success(request, f"Mailbox {username} {action}ed.")
        elif action == "quota":
            form = QuotaForm(request.POST)
            if form.is_valid():
                mailadmin.set_mailbox_quota(username, form.cleaned_data["quota"])
                messages.success(request, f"Quota for {username} set to {form.cleaned_data['quota']}.")
            else:
                messages.error(request, "; ".join(form.errors["quota"]))
    except mailadmin.MailAdminError as exc:
        messages.error(request, str(exc))
    return redirect("webmail:admin_domain_detail", domain=domain)


# --- Client admin ----------------------------------------------------------
# Clients are customers you host mail for: each groups some domains and has
# limits (domains, mailboxes, total storage) enforced by webmail.mailadmin.
# Clients don't log into this section themselves - they only use webmail.

def _client_form_initial(client):
    return {k: client[k] for k in ("name", "max_domains", "max_mailboxes", "max_storage")}


@admin_required
def client_list(request):
    """List clients with their usage against limits, and handle the "add a client" form."""
    form = ClientForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            slug = mailadmin.create_client(**form.cleaned_data)
        except mailadmin.MailAdminError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Client created - now add its domains.")
            return redirect("webmail:admin_client_detail", slug=slug)
    return render(request, "webmail/admin_clients.html", {"clients": mailadmin.list_clients(), "form": form})


@admin_required
def client_detail(request, slug):
    """One client's page: usage, limits, suspend/unsuspend, delete, and its domains."""
    client = mailadmin.get_client(slug)
    if client is None:
        raise Http404("No such client.")

    limits_form = ClientForm(initial=_client_form_initial(client))
    domain_form = AddDomainForm()
    if request.method == "POST":
        form_name = request.POST.get("form_name")
        try:
            if form_name == "limits":
                limits_form = ClientForm(request.POST)
                if limits_form.is_valid():
                    mailadmin.update_client(slug, **limits_form.cleaned_data)
                    messages.success(request, "Client updated.")
                    return redirect("webmail:admin_client_detail", slug=slug)
            elif form_name == "assign":
                domain_form = AddDomainForm(request.POST)
                if domain_form.is_valid():
                    mailadmin.assign_domain(slug, domain_form.cleaned_data["domain"])
                    messages.success(request, f"{domain_form.cleaned_data['domain']} added to this client.")
                    return redirect("webmail:admin_client_detail", slug=slug)
            elif form_name == "unassign":
                domain = _clean_domain(request.POST.get("domain"))
                mailadmin.unassign_domain(slug, domain)
                messages.success(request, f"{domain} removed from this client (its mailboxes were kept).")
                return redirect("webmail:admin_client_detail", slug=slug)
            elif form_name in ("suspend", "unsuspend"):
                mailadmin.set_client_suspended(slug, form_name == "suspend")
                messages.success(request, f"Client {form_name}ed.")
                return redirect("webmail:admin_client_detail", slug=slug)
            elif form_name == "delete":
                mailadmin.delete_client(slug)
                messages.success(request, f"Client {client['name']} deleted. Its domains and mailboxes were kept.")
                return redirect("webmail:admin_clients")
        except mailadmin.MailAdminError as exc:
            messages.error(request, str(exc))

    domains = [d for d in mailadmin.list_domains() if d["domain"] in client["domains"]]
    context = {"client": client, "domains": domains, "limits_form": limits_form, "domain_form": domain_form}
    return render(request, "webmail/admin_client_detail.html", context)
