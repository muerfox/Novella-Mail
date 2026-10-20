"""
View decorators - functions that wrap a view to add a check before it runs.
If you haven't seen this pattern before: `@mail_login_required` above a view
function means "run this check first; only call the real view if it
passes" - see how they're applied in webmail/views.py.
"""
from functools import wraps

from django.conf import settings
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
from django_htmx.http import HttpResponseClientRedirect


def _login_redirect(request):
    """Full-page redirect to login under hx-boost (see views._redirect)."""
    url = reverse("webmail:login")
    if getattr(request, "htmx", False):
        return HttpResponseClientRedirect(url)
    return redirect(url)


def mail_login_required(view_func):
    """
    Require a logged-in mailbox session (both pieces set by login_view: the
    email, and the encrypted password used to re-authenticate to IMAP/SMTP
    on each request). Redirects to the login page otherwise.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.session.get("mail_email") or not request.session.get("mail_password_enc"):
            return _login_redirect(request)
        return view_func(request, *args, **kwargs)

    return wrapper


def admin_required(view_func):
    """
    Require a logged-in session AND that the logged-in email is listed in
    the ADMIN_EMAILS setting (comma-separated env var - see
    novella/settings.py). This is the only access control for the Domains
    admin section: there are no separate "roles" stored anywhere, just this
    allow-list.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        email_addr = request.session.get("mail_email")
        if not email_addr:
            return _login_redirect(request)
        if email_addr.lower() not in settings.ADMIN_EMAILS:
            return HttpResponseForbidden("You don't have access to domain administration.")
        return view_func(request, *args, **kwargs)

    return wrapper
