"""
URL routing for the webmail app - maps each URL path to the view function
in views.py that handles it. Included into the project's URL config at
novella/urls.py. `app_name = "webmail"` is what lets templates/views refer
to these by name as "webmail:login", "webmail:mail", etc. (via Django's
{% url %} tag / reverse()) instead of hardcoding paths - so if a path here
ever changes, nothing else in the codebase needs to.
"""
from django.urls import path

from . import views

app_name = "webmail"

urlpatterns = [
    path("", views.mail_view, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("mail/", views.mail_view, name="mail"),
    path("mail/action/", views.message_action, name="message_action"),
    path("compose/", views.compose_view, name="compose"),
    path("password/", views.change_password_view, name="change_password"),
    path("attachment/", views.attachment_download, name="attachment"),
    # Domain admin (see the "Domain admin" section in views.py) - all
    # gated behind @admin_required, i.e. ADMIN_EMAILS membership.
    path("admin/domains/", views.domain_list, name="admin_domains"),
    path("admin/domains/action/", views.domain_action, name="admin_domain_action"),
    path("admin/domains/<str:domain>/", views.domain_detail, name="admin_domain_detail"),
    path("admin/mailboxes/action/", views.mailbox_action, name="admin_mailbox_action"),
    path("admin/clients/", views.client_list, name="admin_clients"),
    path("admin/clients/<slug:slug>/", views.client_detail, name="admin_client_detail"),
]
