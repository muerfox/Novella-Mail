"""
A Django "context processor": registered in novella/settings.py's TEMPLATES
config, this function's return value gets merged into the context of
*every* template render automatically, so any template can use
{{ mail_email }} or {% if is_admin %} without every view having to pass
them in explicitly (e.g. base.html's top bar shows the logged-in email and
conditionally shows the Domains link this way).
"""
from django.conf import settings


def mail_session(request):
    email_addr = request.session.get("mail_email")
    return {
        "mail_email": email_addr,
        "is_admin": bool(email_addr) and email_addr.lower() in settings.ADMIN_EMAILS,
    }
