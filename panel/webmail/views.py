import math
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from . import crypto, mail, mailcow_api
from .decorators import admin_required, mail_login_required
from .forms import AddDomainForm, AddMailboxForm, ComposeForm, LoginForm


def login_view(request):
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
        else:
            request.session["mail_email"] = email_addr
            request.session["mail_password_enc"] = crypto.encrypt(password)
            request.session.set_expiry(0)
            return redirect("webmail:mail")

    return render(request, "webmail/login.html", {"form": form, "error": error})


def logout_view(request):
    request.session.flush()
    return redirect("webmail:login")


def _get_session_creds(request):
    email_addr = request.session.get("mail_email")
    enc = request.session.get("mail_password_enc")
    password = crypto.decrypt(enc) if enc else None
    return email_addr, password


def _connect_or_bail(request):
    """Returns (server, None) on success, or (None, redirect_response) on failure."""
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
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@mail_login_required
def mail_view(request):
    server, bail = _connect_or_bail(request)
    if bail:
        return bail

    folder = request.GET.get("folder") or "INBOX"
    page = max(_safe_int(request.GET.get("page"), 1), 1)
    uid = request.GET.get("uid")

    folders, msg_list, total, detail = [], [], 0, None
    try:
        folders = mail.list_folders(server)
        msg_list, total = mail.list_messages(server, folder, page=page)
        if uid:
            uid_int = _safe_int(uid, None)
            if uid_int is not None:
                detail = mail.get_message(server, folder, uid_int)
    except mail.MailError as exc:
        messages.error(request, str(exc))
    finally:
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
    }
    return render(request, "webmail/mail.html", context)


@mail_login_required
@require_http_methods(["POST"])
def message_action(request):
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
def attachment_download(request):
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
    filename = att["filename"].replace('"', "")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# --- Domain admin (self-service onboarding via mailcow's API) -------------

@admin_required
def domain_list(request):
    form = AddDomainForm()
    if request.method == "POST":
        form = AddDomainForm(request.POST)
        if form.is_valid():
            domain = form.cleaned_data["domain"]
            try:
                mailcow_api.add_domain(domain, form.cleaned_data["description"])
            except mailcow_api.MailcowAPIError as exc:
                messages.error(request, f"Could not add domain: {exc}")
            else:
                messages.success(
                    request,
                    f"Domain {domain} added. Point its MX record at "
                    f"{settings.MAILCOW_HOSTNAME or 'this server'} to start receiving mail for it "
                    "- see the domain page for the full DNS checklist.",
                )
                return redirect("webmail:admin_domain_detail", domain=domain)

    try:
        domains = mailcow_api.list_domains()
    except mailcow_api.MailcowAPIError as exc:
        messages.error(request, str(exc))
        domains = []

    return render(request, "webmail/admin_domains.html", {"domains": domains, "form": form})


@admin_required
@require_http_methods(["POST"])
def domain_action(request):
    domain = request.POST.get("domain")
    action = request.POST.get("action")
    if action == "delete" and domain:
        try:
            mailcow_api.delete_domain(domain)
            messages.success(request, f"Domain {domain} deleted.")
        except mailcow_api.MailcowAPIError as exc:
            messages.error(request, f"Could not delete domain: {exc}")
    return redirect("webmail:admin_domains")


@admin_required
def domain_detail(request, domain):
    mailbox_form = AddMailboxForm()

    if request.method == "POST" and request.POST.get("form_name") == "mailbox":
        mailbox_form = AddMailboxForm(request.POST)
        if mailbox_form.is_valid():
            try:
                mailcow_api.add_mailbox(
                    domain,
                    mailbox_form.cleaned_data["local_part"],
                    mailbox_form.cleaned_data["password"],
                    mailbox_form.cleaned_data["name"],
                )
            except mailcow_api.MailcowAPIError as exc:
                messages.error(request, f"Could not add mailbox: {exc}")
            else:
                messages.success(request, "Mailbox created.")
                return redirect("webmail:admin_domain_detail", domain=domain)

    elif request.method == "POST" and request.POST.get("form_name") == "dkim":
        try:
            mailcow_api.generate_dkim(domain)
            messages.success(request, "DKIM key generated - add the DNS record below.")
        except mailcow_api.MailcowAPIError as exc:
            messages.error(request, f"Could not generate DKIM key: {exc}")
        return redirect("webmail:admin_domain_detail", domain=domain)

    info, mailboxes = None, []
    try:
        info = mailcow_api.get_domain(domain)
        mailboxes = mailcow_api.list_mailboxes(domain)
    except mailcow_api.MailcowAPIError as exc:
        messages.error(request, str(exc))

    dkim = None
    try:
        dkim = mailcow_api.get_dkim(domain)
    except mailcow_api.MailcowAPIError:
        dkim = None

    context = {
        "domain": domain,
        "info": info,
        "mailboxes": mailboxes,
        "dkim": dkim,
        "mailbox_form": mailbox_form,
        "mailcow_hostname": settings.MAILCOW_HOSTNAME,
    }
    return render(request, "webmail/admin_domain_detail.html", context)


@admin_required
@require_http_methods(["POST"])
def mailbox_action(request):
    domain = request.POST.get("domain")
    username = request.POST.get("username")
    action = request.POST.get("action")
    if action == "delete" and username:
        try:
            mailcow_api.delete_mailbox(username)
            messages.success(request, f"Mailbox {username} deleted.")
        except mailcow_api.MailcowAPIError as exc:
            messages.error(request, f"Could not delete mailbox: {exc}")
    return redirect("webmail:admin_domain_detail", domain=domain)
