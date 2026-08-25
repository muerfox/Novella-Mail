from django.urls import include, path

urlpatterns = [
    path("", include("webmail.urls")),
]
