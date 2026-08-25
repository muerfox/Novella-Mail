"""
Project-level URL config (referenced by ROOT_URLCONF in settings.py). Just
delegates everything to the webmail app's own urls.py - see that file for
the actual routes.
"""
from django.urls import include, path

urlpatterns = [
    path("", include("webmail.urls")),
]
