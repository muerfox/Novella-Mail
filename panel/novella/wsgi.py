"""
WSGI entry point - this is what gunicorn actually runs (see the CMD in
panel/Dockerfile: `gunicorn novella.wsgi:application`). Standard Django
boilerplate, nothing project-specific here.
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "novella.settings")

application = get_wsgi_application()
