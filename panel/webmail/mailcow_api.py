"""
Thin client for mailcow's REST API (https://api.mailcow.email), used for
self-service domain/mailbox onboarding from the panel's "Domains" section.

Reached over the internal `mailcow-network` docker network (see
docker-compose.yml), not the public internet - the API key never leaves the
host. Auth is a static `X-Api-Key` header; mailcow returns HTTP 200 even for
failures, with a `type` field of "success"/"danger"/"error" per item, so
every call here inspects the body rather than trusting the status code.
"""
import requests
from django.conf import settings

_TIMEOUT = 15


class MailcowAPIError(Exception):
    pass


def _headers():
    if not settings.MAILCOW_API_KEY:
        raise MailcowAPIError("MAILCOW_API_KEY is not configured.")
    headers = {"X-Api-Key": settings.MAILCOW_API_KEY, "Accept": "application/json"}
    if settings.MAILCOW_HOSTNAME:
        headers["Host"] = settings.MAILCOW_HOSTNAME
    return headers


def _url(path):
    base = settings.MAILCOW_API_BASE_URL.rstrip("/")
    if not base:
        raise MailcowAPIError("MAILCOW_API_BASE_URL is not configured.")
    return f"{base}{path}"


def _extract_messages(payload):
    items = payload if isinstance(payload, list) else [payload]
    problems = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("danger", "error"):
            msg = item.get("msg")
            if isinstance(msg, list):
                msg = " ".join(str(m) for m in msg)
            problems.append(str(msg))
    return problems


def _request(method, path, json_body=None):
    try:
        resp = requests.request(
            method,
            _url(path),
            headers=_headers(),
            json=json_body,
            timeout=_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        raise MailcowAPIError(f"Could not reach mailcow API: {exc}") from exc

    if resp.status_code == 401:
        raise MailcowAPIError("mailcow API rejected the request (401) - check MAILCOW_API_KEY / API_ALLOW_FROM.")
    if resp.status_code >= 400:
        raise MailcowAPIError(f"mailcow API returned HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise MailcowAPIError(f"mailcow API returned a non-JSON response: {resp.text[:300]}") from exc

    problems = _extract_messages(payload)
    if problems:
        raise MailcowAPIError("; ".join(problems))
    return payload


# --- Domains -----------------------------------------------------------

def list_domains():
    payload = _request("GET", "/api/v1/get/domain/all")
    return payload if isinstance(payload, list) else [payload]


def get_domain(domain):
    payload = _request("GET", f"/api/v1/get/domain/{domain}")
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if isinstance(item, dict) and item.get("domain_name") == domain:
            return item
    return items[0] if items and isinstance(items[0], dict) else None


def add_domain(domain, description=""):
    body = {
        "domain": domain,
        "description": description,
        "aliases": "400",
        "mailboxes": "10",
        "defquota": "3072",
        "maxquota": "10240",
        "quota": "10240",
        "active": "1",
        "backupmx": "0",
        "relay_all_recipients": "0",
        "restart_sogo": "0",
    }
    return _request("POST", "/api/v1/add/domain", body)


def delete_domain(domain):
    return _request("POST", "/api/v1/delete/domain", [domain])


# --- Mailboxes -----------------------------------------------------------

def list_mailboxes(domain):
    payload = _request("GET", f"/api/v1/get/mailbox/all/{domain}")
    return payload if isinstance(payload, list) else [payload]


def add_mailbox(domain, local_part, password, name=""):
    body = {
        "domain": domain,
        "local_part": local_part,
        "name": name,
        "authsource": "mailcow",
        "password": password,
        "password2": password,
        "quota": "3072",
        "active": "1",
        "force_pw_update": "0",
        "tls_enforce_in": "1",
        "tls_enforce_out": "1",
    }
    return _request("POST", "/api/v1/add/mailbox", body)


def delete_mailbox(username):
    return _request("POST", "/api/v1/delete/mailbox", [username])


# --- DKIM -----------------------------------------------------------

def get_dkim(domain):
    payload = _request("GET", f"/api/v1/get/dkim/{domain}")
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not payload or not payload.get("dkim_txt"):
        return None
    return payload


def generate_dkim(domain, selector="dkim", key_size=2048):
    body = {"dkim_selector": selector, "domains": domain, "key_size": key_size}
    return _request("POST", "/api/v1/add/dkim", body)
