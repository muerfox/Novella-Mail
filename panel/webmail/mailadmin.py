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

Clients (customers you host mail for) are the panel's own bookkeeping in
novella-clients.json - DMS has no such concept. A client groups domains and
carries limits (domains, mailboxes, total allocated storage) that the panel
enforces whenever a mailbox, quota, or domain assignment changes.
Suspension is applied to DMS by prefixing the account's password hash with
"!" in postfix-accounts.cf: the hash can then never verify, so IMAP/SMTP
logins fail, while the account (and therefore incoming mail delivery)
stays in place. Unsuspending strips the "!" again - no password reset.
"""
import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.template.defaultfilters import filesizeformat
from django.utils.text import slugify
from passlib.hash import sha512_crypt

ACCOUNTS_FILE = "postfix-accounts.cf"
QUOTAS_FILE = "dovecot-quotas.cf"
CLIENTS_FILE = "novella-clients.json"
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

# The subset of DMS's quota syntax (^([0-9]+(B|k|M|G|T)|0)$) the panel
# accepts: no "0"/unlimited, since every quota has to count against a
# client's finite storage limit.
QUOTA_RE = re.compile(r"^[1-9][0-9]*[MGT]$")
_SIZE_UNITS = {"M": 1024**2, "G": 1024**3, "T": 1024**4}
_HASH_RE = re.compile(r"^(\{[^}]*\})?(!?)(.*)$", re.S)


class MailAdminError(Exception):
    """Raised for admin operations that can't proceed (e.g. a duplicate mailbox/DKIM key)."""


def _config_dir() -> Path:
    """The shared config directory, as seen from *this* (panel) container - see MAIL_CONFIG_DIR in settings.py."""
    return Path(settings.MAIL_CONFIG_DIR)


def _domain_of(email):
    return email.split("@", 1)[-1]


def parse_size(value):
    """Bytes for a quota string like "500M", "3G" or "1T". Raises MailAdminError for anything else."""
    value = (value or "").strip().upper()
    if not QUOTA_RE.match(value):
        raise MailAdminError(f"'{value}' isn't a valid size - use e.g. 500M, 3G or 1T.")
    return int(value[:-1]) * _SIZE_UNITS[value[-1]]


def _quota_bytes(value):
    """Like parse_size, but None for quotas the panel didn't write itself (e.g. "0" set by hand)."""
    try:
        return parse_size(value)
    except MailAdminError:
        return None


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
    # DMS looks a mailbox's quota up with `grep -i "<email>:"` and takes the
    # first hit, so "sales@x.com:" also matches "presales@x.com:". Any such
    # false match is strictly longer, so shortest-first keeps the exact line
    # ahead of it.
    ordered = sorted(quotas.items(), key=lambda kv: (len(kv[0]), kv[0]))
    lines = [f"{email}:{limit}" for email, limit in ordered]
    _atomic_write(_config_dir() / QUOTAS_FILE, "\n".join(lines) + ("\n" if lines else ""))


# --- Clients + suspension (our own bookkeeping - not part of DMS's schema) --

def _read_clients():
    path = _config_dir() / CLIENTS_FILE
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # Refuse rather than fall back to empty: the next write would
            # otherwise wipe every client and its limits.
            raise MailAdminError(f"{CLIENTS_FILE} is unreadable ({exc}) - fix or restore it first.") from exc
    data.setdefault("clients", {})
    data.setdefault("suspended_mailboxes", [])
    return data


def _write_clients(data):
    _atomic_write(_config_dir() / CLIENTS_FILE, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _is_suspended_hash(hashpart):
    return _HASH_RE.match(hashpart).group(2) == "!"


def _with_suspension(hashpart, suspended):
    scheme, _, rest = _HASH_RE.match(hashpart).groups()
    return f"{scheme or ''}{'!' if suspended else ''}{rest}"


def _apply_suspension(accounts, data):
    """
    Set each account's hash marker from the registry (mailbox suspended on
    its own, or its domain's client suspended). Returns whether anything
    changed. Admin mailboxes are never suspended - that would lock every
    admin out of the panel.
    """
    suspended_domains = {
        d for c in data["clients"].values() if c.get("suspended") for d in c["domains"]
    }
    suspended_mailboxes = set(data["suspended_mailboxes"])
    changed = False
    for acc in accounts:
        want = (
            acc["email"] not in settings.ADMIN_EMAILS
            and (acc["email"] in suspended_mailboxes or _domain_of(acc["email"]) in suspended_domains)
        )
        new_hash = _with_suspension(acc["hash"], want)
        if new_hash != acc["hash"]:
            acc["hash"] = new_hash
            changed = True
    return changed


def _sync_suspension(data):
    accounts = _read_accounts()
    if _apply_suspension(accounts, data):
        _write_accounts(accounts)


def is_mailbox_suspended(email):
    email = email.strip().lower()
    return any(a["email"] == email and _is_suspended_hash(a["hash"]) for a in _read_accounts())


def mailbox_usage(email):
    """
    Approximate on-disk size of a mailbox in bytes (mail plus Dovecot's
    index files), read from the mail-data volume mounted read-only at
    settings.MAIL_DATA_DIR. None when that volume isn't mounted.
    """
    root = Path(settings.MAIL_DATA_DIR)
    if not root.is_dir():
        return None
    local_part, _, domain = email.partition("@")
    total = 0
    for dirpath, _dirs, files in os.walk(root / domain / local_part):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _client_summary(slug, client, accounts, quotas, with_usage):
    emails = [a["email"] for a in accounts if _domain_of(a["email"]) in client["domains"]]
    used = None
    if with_usage:
        usages = [mailbox_usage(e) for e in emails]
        used = None if None in usages else sum(usages)
    return {
        **client,
        "slug": slug,
        "domain_count": len(client["domains"]),
        "mailbox_count": len(emails),
        "allocated_bytes": sum(_quota_bytes(quotas.get(e, DEFAULT_QUOTA)) or 0 for e in emails),
        "max_storage_bytes": parse_size(client["max_storage"]),
        "used_bytes": used,
    }


def _check_limits(summary, extra_domains=0, extra_mailboxes=0, extra_bytes=0):
    """Raise MailAdminError if growing this client by the given amounts would exceed any of its limits."""
    name = summary["name"]
    if extra_domains > 0 and summary["domain_count"] + extra_domains > summary["max_domains"]:
        raise MailAdminError(f"{name} is limited to {summary['max_domains']} domain(s).")
    if extra_mailboxes > 0 and summary["mailbox_count"] + extra_mailboxes > summary["max_mailboxes"]:
        raise MailAdminError(f"{name} is limited to {summary['max_mailboxes']} mailbox(es).")
    if extra_bytes > 0 and summary["allocated_bytes"] + extra_bytes > summary["max_storage_bytes"]:
        left = max(summary["max_storage_bytes"] - summary["allocated_bytes"], 0)
        raise MailAdminError(
            f"{name} has {summary['max_storage']} of storage in total - only {filesizeformat(left)} is left to allocate."
        )


def _client_for_domain(domain, data):
    for slug, client in data["clients"].items():
        if domain in client["domains"]:
            return slug, client
    return None, None


def _require_client(data, slug):
    client = data["clients"].get(slug)
    if client is None:
        raise MailAdminError("No such client.")
    return client


def list_clients(with_usage=True):
    data, accounts, quotas = _read_clients(), _read_accounts(), _read_quotas()
    return [
        _client_summary(slug, c, accounts, quotas, with_usage)
        for slug, c in sorted(data["clients"].items(), key=lambda kv: kv[1]["name"].lower())
    ]


def get_client(slug, with_usage=True):
    """One client's settings plus current counts/allocation/usage, or None."""
    data = _read_clients()
    client = data["clients"].get(slug)
    if client is None:
        return None
    return _client_summary(slug, client, _read_accounts(), _read_quotas(), with_usage)


def client_for_domain(domain):
    """Summary of the client owning `domain` (without usage), or None if it's unassigned."""
    data = _read_clients()
    slug, client = _client_for_domain(domain, data)
    if client is None:
        return None
    return _client_summary(slug, client, _read_accounts(), _read_quotas(), False)


def create_client(name, max_domains, max_mailboxes, max_storage):
    """Create a client with no domains yet. Returns its slug (used in URLs)."""
    max_storage = max_storage.strip().upper()
    parse_size(max_storage)
    data = _read_clients()
    base = slugify(name) or "client"
    slug, n = base, 2
    while slug in data["clients"]:
        slug, n = f"{base}-{n}", n + 1
    data["clients"][slug] = {
        "name": name.strip(),
        "max_domains": max_domains,
        "max_mailboxes": max_mailboxes,
        "max_storage": max_storage,
        "suspended": False,
        "domains": [],
        "created": datetime.now(timezone.utc).isoformat(),
    }
    _write_clients(data)
    return slug


def update_client(slug, name, max_domains, max_mailboxes, max_storage):
    """Change a client's name/limits. Limits can't go below what the client already uses."""
    max_storage = max_storage.strip().upper()
    max_storage_bytes = parse_size(max_storage)
    data = _read_clients()
    client = _require_client(data, slug)
    summary = _client_summary(slug, client, _read_accounts(), _read_quotas(), False)
    if max_domains < summary["domain_count"]:
        raise MailAdminError(f"Client already has {summary['domain_count']} domain(s) - remove some first.")
    if max_mailboxes < summary["mailbox_count"]:
        raise MailAdminError(f"Client already has {summary['mailbox_count']} mailbox(es) - remove some first.")
    if max_storage_bytes < summary["allocated_bytes"]:
        raise MailAdminError(
            f"{filesizeformat(summary['allocated_bytes'])} is already allocated to this client's mailboxes - lower their quotas first."
        )
    client.update(name=name.strip(), max_domains=max_domains, max_mailboxes=max_mailboxes, max_storage=max_storage)
    _write_clients(data)


def delete_client(slug):
    """Remove the client record. Its domains and mailboxes stay, just unassigned (and unsuspended)."""
    data = _read_clients()
    _require_client(data, slug)
    del data["clients"][slug]
    _write_clients(data)
    _sync_suspension(data)


def assign_domain(slug, domain):
    """Put `domain` (and every mailbox it already has) under a client, within that client's limits."""
    data = _read_clients()
    client = _require_client(data, slug)
    owner, owner_client = _client_for_domain(domain, data)
    if owner == slug:
        return
    if owner_client is not None:
        raise MailAdminError(f"{domain} already belongs to {owner_client['name']}.")
    accounts, quotas = _read_accounts(), _read_quotas()
    emails = [a["email"] for a in accounts if _domain_of(a["email"]) == domain]
    _check_limits(
        _client_summary(slug, client, accounts, quotas, False),
        extra_domains=1,
        extra_mailboxes=len(emails),
        extra_bytes=sum(_quota_bytes(quotas.get(e, DEFAULT_QUOTA)) or 0 for e in emails),
    )
    client["domains"] = sorted(client["domains"] + [domain])
    _write_clients(data)
    _sync_suspension(data)


def unassign_domain(slug, domain):
    data = _read_clients()
    client = _require_client(data, slug)
    client["domains"] = [d for d in client["domains"] if d != domain]
    _write_clients(data)
    _sync_suspension(data)


def set_client_suspended(slug, suspended):
    """Suspend/unsuspend logins for every mailbox on every domain of this client. Mail keeps arriving."""
    data = _read_clients()
    _require_client(data, slug)["suspended"] = bool(suspended)
    _write_clients(data)
    _sync_suspension(data)


def set_mailbox_suspended(email, suspended):
    """Suspend/unsuspend one mailbox's logins. Mail keeps arriving while suspended."""
    if suspended and email in settings.ADMIN_EMAILS:
        raise MailAdminError(f"{email} is an admin account - suspending it would lock you out of the panel.")
    data = _read_clients()
    entries = set(data["suspended_mailboxes"])
    if suspended:
        entries.add(email)
    else:
        entries.discard(email)
    data["suspended_mailboxes"] = sorted(entries)
    _write_clients(data)
    _sync_suspension(data)


def set_mailbox_quota(email, quota):
    """Change one mailbox's storage quota, within its client's storage limit. Takes effect within seconds."""
    quota = quota.strip().upper()
    new_bytes = parse_size(quota)
    accounts, quotas = _read_accounts(), _read_quotas()
    if not any(a["email"] == email for a in accounts):
        raise MailAdminError(f"{email} doesn't exist.")
    data = _read_clients()
    slug, client = _client_for_domain(_domain_of(email), data)
    if client is not None:
        old_bytes = _quota_bytes(quotas.get(email, DEFAULT_QUOTA)) or 0
        _check_limits(_client_summary(slug, client, accounts, quotas, False), extra_bytes=new_bytes - old_bytes)
    quotas[email] = quota
    _write_quotas(quotas)


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
    its own record) from three sources: the domain-part of every mailbox
    address, any domain that has a DKIM key generated for it, and any
    domain assigned to a client - the latter two even with zero mailboxes
    yet. This is why a domain can legitimately show 0 mailboxes and still
    appear here. Domains owned by a client carry a "client" entry.
    """
    accounts = _read_accounts()
    registry = _read_dkim_registry()
    clients = _read_clients()["clients"]
    domains = {}
    for acc in accounts:
        if "@" not in acc["email"]:
            continue
        domain = acc["email"].split("@", 1)[1]
        entry = domains.setdefault(domain, {"domain": domain, "mailbox_count": 0, "has_dkim": domain in registry})
        entry["mailbox_count"] += 1
    for domain in registry:
        domains.setdefault(domain, {"domain": domain, "mailbox_count": 0, "has_dkim": True})
    # A client can own a domain that has no mailboxes or DKIM key yet.
    for client in clients.values():
        for domain in client["domains"]:
            domains.setdefault(domain, {"domain": domain, "mailbox_count": 0, "has_dkim": False})
    for slug, client in clients.items():
        for domain in client["domains"]:
            domains[domain]["client"] = {"slug": slug, "name": client["name"], "suspended": client.get("suspended", False)}
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

    data = _read_clients()
    for client in data["clients"].values():
        client["domains"] = [d for d in client["domains"] if d != domain]
    data["suspended_mailboxes"] = [e for e in data["suspended_mailboxes"] if _domain_of(e) != domain]
    _write_clients(data)


# --- Mailboxes -----------------------------------------------------------

def list_mailboxes(domain):
    """All mailboxes for one domain, with quota, approximate usage, and suspension state."""
    quotas = _read_quotas()
    individually_suspended = set(_read_clients()["suspended_mailboxes"])
    mailboxes = []
    for a in _read_accounts():
        if _domain_of(a["email"]) != domain:
            continue
        quota = quotas.get(a["email"], DEFAULT_QUOTA)
        quota_bytes = _quota_bytes(quota)
        used = mailbox_usage(a["email"])
        mailboxes.append({
            "username": a["email"],
            "quota": quota,
            "quota_bytes": quota_bytes,
            "used_bytes": used,
            "used_percent": min(round(used * 100 / quota_bytes), 100) if used is not None and quota_bytes else None,
            "suspended": _is_suspended_hash(a["hash"]),
            "individually_suspended": a["email"] in individually_suspended,
            "is_admin": a["email"] in settings.ADMIN_EMAILS,
        })
    return mailboxes


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
    quota = quota.strip().upper()
    quota_bytes = parse_size(quota)
    accounts = _read_accounts()
    if any(a["email"] == email for a in accounts):
        raise MailAdminError(f"{email} already exists.")

    quotas = _read_quotas()
    data = _read_clients()
    slug, client = _client_for_domain(_domain_of(email), data)
    if client is not None:
        _check_limits(_client_summary(slug, client, accounts, quotas, False), extra_mailboxes=1, extra_bytes=quota_bytes)

    # Quota first, so DMS never sees the new account without its quota.
    quotas[email] = quota
    _write_quotas(quotas)

    accounts.append({"email": email, "hash": "{SHA512-CRYPT}" + sha512_crypt.hash(password)})
    _apply_suspension(accounts, data)
    _write_accounts(accounts)


def delete_mailbox(username):
    """Remove one mailbox's account + quota entries. Doesn't delete already-delivered mail (see delete_domain's note)."""
    accounts = [a for a in _read_accounts() if a["email"] != username]
    _write_accounts(accounts)

    quotas = _read_quotas()
    quotas.pop(username, None)
    _write_quotas(quotas)

    data = _read_clients()
    if username in data["suspended_mailboxes"]:
        data["suspended_mailboxes"].remove(username)
        _write_clients(data)


def change_mailbox_password(email, current_password, new_password):
    """
    Let a mailbox owner change their own password. Verifies `current_password`
    against the stored hash (not via IMAP), so this works even if the mail
    server hasn't finished its ~2s reload yet. Re-applies any suspension
    marker afterward so changing a password never accidentally unsuspends
    an account. The plaintext is never written to disk.
    """
    email = email.strip().lower()
    if len(new_password) < 8:
        raise MailAdminError("Use at least 8 characters.")

    accounts = _read_accounts()
    for acc in accounts:
        if acc["email"] != email:
            continue
        if _is_suspended_hash(acc["hash"]):
            raise MailAdminError("This mailbox is suspended.")
        stored = _HASH_RE.match(acc["hash"]).group(3)
        if not sha512_crypt.verify(current_password, stored):
            raise MailAdminError("Current password is incorrect.")
        if sha512_crypt.verify(new_password, stored):
            raise MailAdminError("Choose a password different from the current one.")

        data = _read_clients()
        acc["hash"] = "{SHA512-CRYPT}" + sha512_crypt.hash(new_password)
        _apply_suspension(accounts, data)
        _write_accounts(accounts)
        return

    raise MailAdminError(f"{email} doesn't exist.")


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
