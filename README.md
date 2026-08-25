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
- **Both panels are private**: the Novella panel on `127.0.0.1:3009` and the
  mailcow admin UI on `127.0.0.1:3006`, reachable via SSH tunnel or your own
  reverse proxy — never exposed directly to the internet.

## Quickstart

```
git clone --recurse-submodules <this-repo-url>
cd Novella-Mail
./setup.sh
docker compose up -d
```

That last command is the only one you need for day-to-day up/down after
setup — the root `docker-compose.yml` brings up the mail engine and the
panel together as one project. Everything needed to do that is vendored in
this repo (mailcow-dockerized as a pinned git submodule, the panel's own
source, htmx vendored with no CDN dependency); the only thing that still
needs internet access is the first `docker compose up -d` itself, to
pull/build the container images.

Then see [docs/SETUP.md](docs/SETUP.md) for DNS records, firewall rules,
reaching the panels, first login, and the TLS tradeoff that comes with
keeping everything localhost-only.

## Layout

- `docker-compose.yml` — the single entry point; `include:`s the two below
  into one project. Reads variables from a generated root `.env` (see
  `sync-env.sh`), not from `mailcow.conf`/`panel/.env` directly.
- `setup.sh` — checks out the mailcow-dockerized submodule if needed,
  configures the port split above, generates `panel/.env`, and runs
  `sync-env.sh`.
- `sync-env.sh` — regenerates the root `.env` from `mailcow-dockerized/
  mailcow.conf` + `panel/.env`. Run it any time you hand-edit either of
  those two files - `docker compose` only ever reads the merged result.
- `mailcow-dockerized/` — the mail engine, vendored as a git submodule
  (pinned to a release tag; `.gitmodules` has the upstream URL). Updates via
  the normal `git pull` / `./update.sh` inside it, then commit the new
  pinned commit in this repo.
- `panel/` — the Django + HTMX panel (see `panel/webmail/` for the app).
- `docs/SETUP.md` — full walkthrough.
