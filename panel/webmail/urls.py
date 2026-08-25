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
    path("attachment/", views.attachment_download, name="attachment"),
    path("admin/domains/", views.domain_list, name="admin_domains"),
    path("admin/domains/action/", views.domain_action, name="admin_domain_action"),
    path("admin/domains/<str:domain>/", views.domain_detail, name="admin_domain_detail"),
    path("admin/mailboxes/action/", views.mailbox_action, name="admin_mailbox_action"),
]
