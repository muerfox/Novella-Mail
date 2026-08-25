"""
Symmetric encryption for the mailbox password we have to keep around for the
lifetime of a login session so we can re-authenticate to IMAP/SMTP on every
request (there's no way around this - it's how every IMAP-backed webmail
client, e.g. Roundcube, works).

The key is derived from SECRET_KEY so nothing extra needs to be provisioned,
and it keeps the password out of the sessions table in plaintext.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
