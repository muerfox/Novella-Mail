"""
Django settings for the Novella-Mail panel.

If you're new to Django: this file is read once when the app starts and
configures everything from "which database to use" to "which URLs exist"
(ROOT_URLCONF, below, points at novella/urls.py, which is where routing
actually happens). Almost every setting here is filled in from environment
variables (via `os.environ.get(...)`) rather than hardcoded, because this
same code runs unmodified for anyone who deploys it - the actual values
live in `.env` at the repo root (see `.env.example` for what each one
means and setup.sh, which generates `.env` for you) or straight in the
OS environment - `.env` is optional.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Path to the panel/ directory (two levels up from this file: settings.py
# -> novella/ -> panel/). Used below to find .env and to store the SQLite
# database and collected static files inside the container.
BASE_DIR = Path(__file__).resolve().parent.parent

# Reads panel/.env, then the repo-root .env, if they exist (useful for
# local development outside Docker) and sets each KEY=value line as a real
# environment variable, but only for keys that aren't already set - so the
# OS environment (including container env vars from docker-compose.yml)
# always wins, and with no .env at all everything comes from the OS.
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")


def _env_bool(name, default=False):
    """Read an env var as a boolean. Accepts 1/true/yes/y/on (any case)."""
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_list(name, default=""):
    """Read a comma-separated env var into a list of trimmed, non-empty strings."""
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --- Core Django settings --------------------------------------------------

# Used by Django to sign session cookies, CSRF tokens, etc. MUST be secret
# and MUST be different in every real deployment - setup.sh generates a
# random one automatically. The fallback here only matters if someone runs
# the app without ever going through setup.sh (e.g. local dev) - never rely
# on it for anything real.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")

# Django's own debug mode: verbose error pages with full tracebacks, no
# static file caching, etc. Must be False for anything reachable by anyone
# other than you - a debug page can leak your SECRET_KEY and env vars.
DEBUG = _env_bool("DJANGO_DEBUG", False)

# Which Host headers Django will accept a request for - a basic defense
# against HTTP Host header attacks. Needs to include whatever hostname(s)
# you actually reach the panel through.
ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")

# The Django apps this project uses. We deliberately do NOT include
# django.contrib.auth or django.contrib.admin: there's no separate "panel
# user" concept here on purpose - you log in with a real mailbox
# email+password (checked via a live IMAP login, see webmail/views.py),
# so there's no user database to manage.
INSTALLED_APPS = [
    "django.contrib.sessions",     # server-side session storage (login state)
    "django.contrib.messages",     # one-shot "flash" messages (success/error banners)
    "django.contrib.staticfiles",  # collects/serves CSS, JS, the vendored htmx.js
    "django_htmx",                 # small helper for detecting/handling htmx requests
    "webmail",                     # the actual app - see webmail/ for everything it does
]

# Middleware runs, in this order, on every request (top-to-bottom) and
# again in reverse on every response. A few worth calling out:
#   - WhiteNoiseMiddleware serves the static files directly from the app
#     process, so we don't need a separate nginx/Apache just for that.
#   - HtmxMiddleware adds request.htmx, used by templates/views to know
#     whether a request came from an htmx-powered interaction.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "novella.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,  # auto-discover templates in webmail/templates/webmail/
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
                # Makes {{ mail_email }} and {{ is_admin }} available in
                # every template without passing them explicitly from every
                # view - see webmail/context_processors.py.
                "webmail.context_processors.mail_session",
            ],
        },
    },
]

WSGI_APPLICATION = "novella.wsgi.application"

# SQLite is enough here: the only things stored in the database are Django
# sessions (login state) - there's no other model in this project. All the
# actual mail/domain/mailbox data lives in docker-mailserver's own volumes,
# managed as plain files (see webmail/mailadmin.py), not in this database.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DJANGO_DB_PATH", str(BASE_DIR / "data" / "db.sqlite3")),
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TZ", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # WhiteNoise's storage: hashes each static file's content into its
    # filename (cache-busting) and pre-compresses it, so the app can serve
    # its own static files efficiently with no separate web server needed.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Mail connection settings (used by webmail/mail.py) --------------------
# These are the IMAP/SMTP settings the panel uses to act as a mail *client*
# - reading and sending mail exactly the way Thunderbird or any phone mail
# app would, just talking to them over Docker's internal network instead of
# the public internet. "mailserver" below is not a placeholder: it's the
# actual hostname docker-compose.yml gives the mail container, resolvable
# by name from inside the panel container because they're on the same
# Docker network by default.
MAIL_IMAP_HOST = os.environ.get("MAIL_IMAP_HOST", "mailserver")
MAIL_IMAP_PORT = int(os.environ.get("MAIL_IMAP_PORT", "993"))
MAIL_SMTP_HOST = os.environ.get("MAIL_SMTP_HOST", "mailserver")
MAIL_SMTP_PORT = int(os.environ.get("MAIL_SMTP_PORT", "587"))
# docker-mailserver's default cert is self-signed (see .env.example's
# SSL_TYPE), which normal TLS verification would reject. Set this to false
# once you've configured a real certificate (docs/SETUP.md section 7).
MAIL_TLS_INSECURE = _env_bool("MAIL_TLS_INSECURE", False)

MESSAGES_PER_PAGE = 25

# --- Domain admin (self-service onboarding, file-based - see webmail/mailadmin.py) ---
# Mailbox emails (comma separated) allowed to see the "Domains" section of
# the panel - i.e. who's trusted to add/remove domains, mailboxes, and DKIM
# keys for the whole server, not just manage their own inbox. Lower-cased
# for a case-insensitive comparison against the logged-in email.
ADMIN_EMAILS = [e.lower() for e in _env_list("ADMIN_EMAILS", "")]
# Path (inside *this* container) to the volume shared with docker-mailserver.
# docker-compose.yml mounts the same underlying volume into the mailserver
# container at /tmp/docker-mailserver/ - writing a file here is really
# writing straight into docker-mailserver's own config directory. Its
# built-in change-detector notices edits (content-hash polling, ~2s) and
# reloads Postfix/Dovecot/Rspamd on its own - no API calls, no `docker
# exec`, no elevated Docker permissions needed for any of this.
MAIL_CONFIG_DIR = os.environ.get("MAIL_CONFIG_DIR", "/mailserver-config")
# docker-mailserver's mail-data volume, mounted read-only - only used to
# show approximate per-mailbox storage usage in the admin pages.
MAIL_DATA_DIR = os.environ.get("MAIL_DATA_DIR", "/mail-data")
# The mail server's own hostname (e.g. mail.example.com) - shown in the
# panel's DNS-record checklist for each domain you add.
MAIL_DOMAIN = os.environ.get("MAIL_DOMAIN", "")

# Only send cookies over HTTPS once we're not in local debug mode - in
# production that HTTPS connection is your reverse proxy's job, not
# Django's (see the reverse-proxy section below).
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_ENGINE = "django.contrib.sessions.backends.db"
# Log out when the browser closes rather than persisting indefinitely -
# reasonable default for a mail panel; note it's the *browser* closing,
# not a fixed timeout, so a tab left open stays logged in.
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

LOGIN_URL = "webmail:login"

# --- Reverse proxy -----------------------------------------------------
# The panel only binds 127.0.0.1 on PANEL_PORT (see docker-compose.yml) - it's
# designed to sit behind your own reverse proxy (nginx/Caddy/Traefik/
# whatever you already run), which handles the actual public HTTPS. That
# proxy MUST set the X-Forwarded-Proto header (and should set
# X-Forwarded-Host) on requests it forwards here, or Django has no way to
# know the original request was HTTPS - without this, secure cookies and
# generated URLs silently break behind a proxy. Trusting these headers is
# safe specifically *because* the panel isn't reachable except through
# that proxy: nothing else can reach 127.0.0.1:PANEL_PORT to spoof them.
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Comma separated, e.g. https://mail-panel.yourdomain.com - the public
# origin(s) your reverse proxy serves this panel under. Django's CSRF
# protection compares the browser's Origin header against this list for
# unsafe requests (POST, etc.); without it, login/compose/every form
# submission gets rejected once you're behind a proxy under a different
# hostname than Django itself sees.
CSRF_TRUSTED_ORIGINS = _env_list("CSRF_TRUSTED_ORIGINS", "")
