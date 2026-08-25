from functools import wraps

from django.conf import settings
from django.http import HttpResponseForbidden
from django.shortcuts import redirect


def mail_login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.session.get("mail_email") or not request.session.get("mail_password_enc"):
            return redirect("webmail:login")
        return view_func(request, *args, **kwargs)

    return wrapper


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        email_addr = request.session.get("mail_email")
        if not email_addr:
            return redirect("webmail:login")
        if email_addr.lower() not in settings.ADMIN_EMAILS:
            return HttpResponseForbidden("You don't have access to domain administration.")
        return view_func(request, *args, **kwargs)

    return wrapper
