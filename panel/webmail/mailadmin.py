"""
File-based admin layer for docker-mailserver (DMS). Reads/writes DMS's own
config files directly in the shared volume (settings.MAIL_CONFIG_DIR, bind-
mounted into both the panel and mailserver containers at
/tmp/docker-mailserver/ on the mailserver side). DMS's own change-detector
polls these files every ~2s (content-hash based) and reloads Postfix/
Dovecot/Rspamd automatically - no docker exec, no Docker socket access, no
network API, no credentials to manage for admin operations at all.

File formats confirmed against docker-mailserver source (accounts/quotas/
change-detection: target/scripts/helpers/database/manage/postfix-accounts.sh,
target/scripts/helpers/change-detection.sh; DKIM: target/bin/rspamd-dkim,
docs/content/config/best-practices/dkim_dmarc_spf.md):

- postfix-accounts.cf: "email|{SHA512-CRYPT}$6$salt$hash" per line.
- dovecot-quotas.cf: "email:limit" per line (e.g. "3G").
- Domains need no separate registration - DMS derives its accepted domain
  list purely from the domain-part of every address in these files.
- DKIM uses Rspamd's module (not OpenDKIM - that one only loads keys at
  container startup, so it can't be hot-provisioned without docker exec,
  which is exactly what this whole approach avoids). Rspamd's DKIM *is*
  covered by change-detection, so writing the key + signing config here is
  enough for it to take effect within a few seconds.
"""
import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from passlib.hash import sha512_crypt

ACCOUNTS_FILE = "postfix-accounts.cf"
QUOTAS_FILE = "dovecot-quotas.cf"
DKIM_REGISTRY_FILE = "novella-dkim-registry.json"
DKIM_KEY_DIR = "rspamd/dkim"
DKIM_SIGNING_CONF = "rspamd/override.d/dkim_signing.conf"

# Path prefix as seen *inside the mailserver container* (its mount point for
# this same shared volume) - this is what has to go into dkim_signing.conf's
# "path" values, not our own container's MAIL_CONFIG_DIR mount point.
DMS_CONFIG_PREFIX = "/tmp/docker-mailserver"

DEFAULT_QUOTA = "3G"
DEFAULT_SELECTOR = "mail"
DEFAULT_KEY_SIZE = 2048


class MailAdminError(Exception):
    """Raised for admin operations that can't proceed (e.g. a duplicate mailbox/DKIM key)."""


def _config_dir() -> Path:
    """The shared config directory, as seen from *this* (panel) container - see MAIL_CONFIG_DIR in settings.py."""
    return Path(settings.MAIL_CONFIG_DIR)


def _atomic_write(path: Path, content: str):
    """
    Write a file atomically: write to a temp file in the same directory,
    then rename it over the target. `os.replace` is an atomic rename on
    POSIX filesystems, so anything reading `path` (in particular
    docker-mailserver's own change-detector, polling this exact file from
    the *other* container) only ever sees either the complete old content
    or the complete new content - never a half-written file mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(content)
    os.replace(tmp, path)


# --- postfix-accounts.cf -----------------------------------------------
# docker-mailserver's own file listing every mailbox: one "email|hash" line
# per account. This is the single source of truth for "which mailboxes
# exist" - list_domains() below derives the domain list from it too.

def _read_accounts():
    path = _config_dir() / ACCOUNTS_FILE
    if not path.exists():
        return []
    accounts = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        email, _, hashpart = line.partition("|")
        accounts.append({"email": email, "hash": hashpart})
    return accounts


def _write_accounts(accounts):
    lines = [f"{a['email']}|{a['hash']}" for a in accounts]
    _atomic_write(_config_dir() / ACCOUNTS_FILE, "\n".join(lines) + ("\n" if lines else ""))


# --- dovecot-quotas.cf ---------------------------------------------------
# One "email:limit" line per mailbox with a non-default quota. A mailbox
# with no line here just isn't quota-limited by this file (docker-mailserver
# may still apply its own default) - add_mailbox() below always writes one
# so every mailbox created through the panel has an explicit quota.

def _read_quotas():
    path = _config_dir() / QUOTAS_FILE
    if not path.exists():
        return {}
    quotas = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        email, _, limit = line.partition(":")
        quotas[email] = limit
    return quotas


def _write_quotas(quotas):
    lines = [f"{email}:{limit}" for email, limit in quotas.items()]
    _atomic_write(_config_dir() / QUOTAS_FILE, "\n".join(lines) + ("\n" if lines else ""))


# --- DKIM registry (our own bookkeeping - not part of DMS's schema) -----

def _read_dkim_registry():
    path = _config_dir() / DKIM_REGISTRY_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_dkim_registry(registry):
    _atomic_write(_config_dir() / DKIM_REGISTRY_FILE, json.dumps(registry, indent=2, sort_keys=True) + "\n")


def _render_dkim_signing_conf(registry):
    """
    Render Rspamd's dkim_signing.conf (a UCL config file - similar spirit
    to JSON, but Rspamd's own syntax) from scratch, from the full DKIM
    registry. This has to be a full read-modify-write of the *entire*
    `domain { ... }` block every time (there's no way to just append one
    domain's entry) since Rspamd only supports one such block per config
    file - see the module docstring above for where this format was
    confirmed. Domain names are used as quoted string keys ("example.com")
    rather than bare identifiers, since bare UCL identifiers have stricter
    character rules than DNS domain names do.
    """
    lines = [
        "enabled = true;",
        "sign_authenticated = true;",
        "sign_local = false;",
        "try_fallback = false;",
        'use_domain = "header";',
        "use_redis = false;",
        "use_esld = true;",
        "allow_username_mismatch = true;",
        "check_pubkey = true;",
        "",
        "domain {",
    ]
    for domain, info in sorted(registry.items()):
        key_path = f"{DMS_CONFIG_PREFIX}/{DKIM_KEY_DIR}/rsa-{info['key_size']}-{info['selector']}-{domain}.private.txt"
        lines.append(f'    "{domain}" {{')
        lines.append(f'        path = "{key_path}";')
        lines.append(f'        selector = "{info["selector"]}";')
        lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _dns_txt_value(public_key) -> str:
    """
    Build the DKIM DNS TXT record value for a public key, in the standard
    "v=DKIM1; k=rsa; p=<base64>" format any DNS provider expects - this is
    what gets shown in the panel for the user to copy into their DNS zone.
    """
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return f"v=DKIM1; k=rsa; p={base64.b64encode(der).decode()}"


# --- Domains (derived - DMS has no domain-registration file) -------------

def list_domains():
    """
    Every domain the panel knows about, derived (not stored anywhere as
    its own record) from two sources: the domain-part of every mailbox
    address, and any domain that has a DKIM key generated for it even with
    zero mailboxes yet. This is why a domain can legitimately show 0
    mailboxes and still appear here.
    """
    accounts = _read_accounts()
    registry = _read_dkim_registry()
    domains = {}
    for acc in accounts:
        if "@" not in acc["email"]:
            continue
        domain = acc["email"].split("@", 1)[1]
        entry = domains.setdefault(domain, {"domain": domain, "mailbox_count": 0, "has_dkim": domain in registry})
        entry["mailbox_count"] += 1
    for domain in registry:
        domains.setdefault(domain, {"domain": domain, "mailbox_count": 0, "has_dkim": True})
    return sorted(domains.values(), key=lambda d: d["domain"])


def get_domain(domain):
    """Look up one domain's summary (mailbox count, whether it has DKIM). Never raises for an unknown domain - see the comment below."""
    for d in list_domains():
        if d["domain"] == domain:
            return d
    # Not provisioned yet is still a valid state to view (e.g. generate
    # DKIM before adding the first mailbox) - domains aren't pre-registered.
    return {"domain": domain, "mailbox_count": 0, "has_dkim": False}


def delete_domain(domain):
    """
    Remove every mailbox for `domain` and its DKIM key registration (if
    any). Does *not* delete the mail already delivered (still sitting in
    the mail-data volume) or the DKIM private key file on disk - both are
    left in place deliberately, since removing account/config entries
    should never be the thing that silently destroys real data.
    """
    accounts = [a for a in _read_accounts() if a["email"].split("@", 1)[-1] != domain]
    _write_accounts(accounts)

    quotas = {e: q for e, q in _read_quotas().items() if e.split("@", 1)[-1] != domain}
    _write_quotas(quotas)

    registry = _read_dkim_registry()
    if domain in registry:
        del registry[domain]
        _write_dkim_registry(registry)
        _atomic_write(_config_dir() / DKIM_SIGNING_CONF, _render_dkim_signing_conf(registry))


# --- Mailboxes -----------------------------------------------------------

def list_mailboxes(domain):
    """All mailboxes for one domain, with their quota."""
    quotas = _read_quotas()
    return [
        {"username": a["email"], "quota": quotas.get(a["email"], DEFAULT_QUOTA)}
        for a in _read_accounts()
        if a["email"].split("@", 1)[-1] == domain
    ]


def add_mailbox(domain, local_part, password, quota=DEFAULT_QUOTA):
    """
    Create a new mailbox. The password is hashed with passlib's
    sha512_crypt (produces the exact same `$6$salt$hash` format as
    `doveadm pw -s SHA512-CRYPT`, verified byte-for-byte against it while
    building this) - the plaintext password is never written to disk.
    docker-mailserver will start accepting mail for this address (and
    therefore this domain, if it's new) within a few seconds, once its
    change-detector notices the file changed - no restart needed.
    """
    email = f"{local_part}@{domain}".strip().lower()
    accounts = _read_accounts()
    if any(a["email"] == email for a in accounts):
        raise MailAdminError(f"{email} already exists.")

    accounts.append({"email": email, "hash": "{SHA512-CRYPT}" + sha512_crypt.hash(password)})
    _write_accounts(accounts)

    quotas = _read_quotas()
    quotas[email] = quota
    _write_quotas(quotas)


def delete_mailbox(username):
    """Remove one mailbox's account + quota entries. Doesn't delete already-delivered mail (see delete_domain's note)."""
    accounts = [a for a in _read_accounts() if a["email"] != username]
    _write_accounts(accounts)

    quotas = _read_quotas()
    quotas.pop(username, None)
    _write_quotas(quotas)


# --- DKIM (Rspamd module - hot-reloadable via change-detection) ---------

def get_dkim(domain):
    """This domain's DKIM info (selector, key size, DNS TXT value), or None if it doesn't have one yet."""
    return _read_dkim_registry().get(domain)


def generate_dkim(domain, selector=DEFAULT_SELECTOR, key_size=DEFAULT_KEY_SIZE):
    """
    Generate a new RSA keypair for `domain`, write the private key where
    Rspamd expects it, and rewrite dkim_signing.conf so Rspamd actually
    starts signing outgoing mail for this domain with it. One key per
    domain for now (regenerating means deleting the domain first) - this
    is deliberately simple rather than supporting key rotation, which
    would need a way to publish both the old and new DNS records during a
    transition period.
    """
    registry = _read_dkim_registry()
    if domain in registry:
        raise MailAdminError(f"{domain} already has a DKIM key (selector {registry[domain]['selector']}).")

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    key_path = _config_dir() / DKIM_KEY_DIR / f"rsa-{key_size}-{selector}-{domain}.private.txt"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(private_pem)
    # Written from outside the container as our own UID; Rspamd's internal
    # user needs read access and we can't chown to its in-container UID
    # from here, so make it world-readable rather than silently broken.
    os.chmod(key_path, 0o644)

    dns_txt = _dns_txt_value(private_key.public_key())

    registry[domain] = {
        "selector": selector,
        "key_size": key_size,
        "created": datetime.now(timezone.utc).isoformat(),
        "dns_txt": dns_txt,
    }
    _write_dkim_registry(registry)
    _atomic_write(_config_dir() / DKIM_SIGNING_CONF, _render_dkim_signing_conf(registry))

    return registry[domain]
