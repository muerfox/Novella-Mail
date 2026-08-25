from django.conf import settings


def mail_session(request):
    email_addr = request.session.get("mail_email")
    return {
        "mail_email": email_addr,
        "is_admin": bool(email_addr) and email_addr.lower() in settings.ADMIN_EMAILS,
    }
