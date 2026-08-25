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
    """
    Build a Fernet (symmetric encryption) instance from Django's own
    SECRET_KEY. SHA-256 hashing it first just reshapes it into the exact
    32 raw bytes Fernet requires - SECRET_KEY itself is already meant to be
    secret, so this doesn't add or remove any real security, it's purely a
    format conversion so we don't need a second secret to provision/store.
    """
    key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    """Encrypt a string (e.g. a mailbox password) into an opaque token safe to store in a session."""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str | None:
    """Reverse encrypt(). Returns None (rather than raising) for a missing/corrupt/tampered token."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
