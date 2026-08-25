# Novella-Mail

A self-hosted, multi-domain mail server, deployed via Docker, fronted by a
custom Django + HTMX panel for sending, reading, and managing mail —
responsive on desktop and phone, no shell access needed for day-to-day use.
Add any domain whose MX you point here, straight from the panel, and it
starts serving mail for it.

Two pieces:

- **Mail engine** — [mailcow-dockerized](https://github.com/mailcow/mailcow-dockerized)
  (Postfix, Dovecot, Rspamd, ClamAV). Multi-domain by design. SOGo webmail is
  disabled; mailcow's own admin UI stays up as a secondary panel for quota/
  spam settings the custom panel doesn't cover yet.
- **Panel** (`panel/`) — the custom Django + HTMX app, a plain IMAP/SMTP
  client (same protocols any desktop mail client uses) with its own login
  (your mailbox email + password), inbox/folder view, compose with
  attachments, reply/reply-all/forward, HTML mail rendered through a
  sanitized, sandboxed iframe, and a **Domains** section (for allow-listed
  admin accounts) to add domains/mailboxes/DKIM keys via mailcow's API —
  no need to touch the mailcow admin UI after the first domain.

Port layout:

- **Mail ports are public**: SMTP (25), SMTPS (465), Submission (587), IMAP
  (143), IMAPS (993), POP3 (110/995), Sieve (4190) bind on all interfaces, as
  a mail server needs to.
- **Both panels are private**: the Novella panel on `127.0.0.1:3007` and the
  mailcow admin UI on `127.0.0.1:3006`, reachable via SSH tunnel or your own
  reverse proxy — never exposed directly to the internet.

## Quickstart

```
./setup.sh
```

Then see [docs/SETUP.md](docs/SETUP.md) for DNS records, firewall rules,
reaching the panels, first login, and the TLS tradeoff that comes with
keeping everything localhost-only.

## Layout

- `setup.sh` — fetches mailcow-dockerized, configures the port split above,
  and generates `panel/.env`.
- `panel/` — the Django + HTMX panel (see `panel/webmail/` for the app).
- `docs/SETUP.md` — full walkthrough.
- `mailcow-dockerized/` — created by `setup.sh`, not committed (it manages
  its own updates via `git pull` / `update.sh`).
