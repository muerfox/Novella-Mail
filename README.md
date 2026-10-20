# Novella-Mail

**Your own mail server, for as many domains as you want, with a clean web
panel to manage it — all in Docker, running on hardware you control.**

If you've ever wanted to host `you@yourdomain.com` (or mail for a dozen
side projects and clients, cPanel-style) without paying per-mailbox or
trusting a third party with your mail, this is that: a real Postfix +
Dovecot mail server under the hood, fronted by a purpose-built panel for
sending, reading, and managing mail day to day, plus adding new domains
and mailboxes yourself whenever you need to.

It's built to be approachable even if you've never run a mail server
before — `./setup.sh` asks a couple of questions and can walk you all the
way to a working first login, and the panel is where you'll spend nearly
all your time afterward. You shouldn't need to touch the mail server's
internals unless you want to.

## What you get

- **A real mail server** — [docker-mailserver](https://github.com/docker-mailserver/docker-mailserver)
  (Postfix + Dovecot + Rspamd), the same battle-tested software real mail
  hosts run, not a toy reimplementation.
- **Multi-domain, out of the box** — host `you@example.com`,
  `hello@side-project.io`, and `team@client-work.dev` on one server. Add a
  domain, point its MX record here, and it's live — no per-domain
  container, no restart.
- **A clean, responsive web panel** — read, search, star, delete,
  reply, forward, and compose mail with attachments, from your desktop or
  your phone. Built with Django + [htmx](https://htmx.org), so it feels
  fast without shipping a pile of JavaScript.
- **Self-service domain admin** — add domains, create mailboxes, and
  generate DKIM keys straight from the panel, with the exact DNS records
  to publish shown right there. No separate admin tool to learn.
- **Two containers, one command** — `docker compose up -d` and you're
  running. No vendored source to keep in sync, no config generator to run.
- **Sane security defaults** — the panel binds to `127.0.0.1` only (put
  your own reverse proxy in front of it), incoming HTML mail is sanitized
  and sandboxed before it's ever rendered, and the mail server refuses to
  act as an open relay by default.

## Is this for you?

Novella-Mail is for anyone comfortable running a Docker container on a
server (a cheap VPS is plenty) who wants real control over their email —
indie hackers hosting mail for several projects, small teams tired of
per-seat mailbox pricing, or anyone who just wants to own their inbox.
You'll need:

- A server with a public IP address (any budget VPS works).
- A domain (or several) you control DNS for.
- About fifteen minutes for the initial setup.

You do **not** need prior mail server experience — that's the whole point
of the panel.

## Quickstart

```bash
git clone https://github.com/muerfox/Novella-Mail.git
cd Novella-Mail
./setup.sh
```

`setup.sh` asks for your mail server's hostname (e.g. `mail.example.com`)
and timezone, generates a `.env` file, and then offers to do the rest for
you: bring the stack up, create your first mailbox, and unlock the admin
panel for it. Answer yes to all three and you'll have a working login by
the end of the script. Prefer to do it by hand, or want the full DNS/
firewall walkthrough? See **[docs/SETUP.md](docs/SETUP.md)**.

## Using the panel

### Logging in

There's no separate "panel account" — you log in with a real mailbox's
email and password (the panel checks it by trying an actual IMAP login).
Your first mailbox is created via:
```bash
docker compose exec panel python manage.py createmailbox you@yourdomain.com
```
(`setup.sh` offers to run this for you automatically.)

### Reading and organizing mail

The main view is a classic three-pane layout on desktop (folders, message
list, open message) that collapses to a single scrollable view on mobile.
Click a folder to browse it, click a message to read it. Each message has
quick actions for marking read/unread, starring, and deleting (moves to
Trash if your account has one, otherwise deletes permanently). HTML emails
are rendered through a sanitizer and a sandboxed frame, so opening a
message never runs anything an email sender put in it.

### Sending mail

Hit **Compose** for a new message, or **Reply**, **Reply all**, or
**Forward** from an open message — the form pre-fills the quoted original
and threading headers for you. Attachments work both ways: attach files
when composing, and download attachments from received mail.

### Changing your password

Any mailbox can change its own password from **Password** in the top bar
(current password + new password twice). You stay signed in; other mail
apps need the new password after the mail server picks it up (a few seconds).

### Adding a domain

Open **Domains** in the top bar (only visible to accounts listed in
`ADMIN_EMAILS` — see below) and type a domain name. There's no separate
"create" step: the mail server figures out which domains it's hosting
purely from which mailboxes exist, so typing a domain just takes you to
its page, ready to set up.

From that page you can:
1. **See the DNS records to publish** — MX, SPF, DMARC, and (once
   generated) DKIM, filled in with your actual values, ready to copy into
   whatever DNS provider that domain uses.
2. **Generate a DKIM key** with one click — this is what lets receiving
   mail servers verify your outgoing mail wasn't forged, and matters a lot
   for actually landing in inboxes instead of spam folders.
3. **Add mailboxes** for the domain — an address and a password, and it's
   receiving mail within a few seconds.

### Clients, quotas and suspension

Open **Clients** in the top bar to create a client (a customer you host
mail for) with limits on domains, mailboxes and total storage, then add
its domains. From there you can raise or lower limits, set each mailbox's
quota, see how much storage is used, and suspend a single mailbox or a
whole client (logins stop, incoming mail is still accepted). Clients
themselves just log into webmail with their mailbox. See
[docs/SETUP.md](docs/SETUP.md) section 10.

Deleting a domain removes it and all its mailboxes (there's a confirmation
prompt — it's not reversible from the panel, though the actual delivered
mail and DKIM key file are left on disk rather than wiped, just in case).

### Becoming a domain admin

The very first account has to unlock this for itself, since there's no
separate admin login: edit `.env`, add your mailbox's email to
`ADMIN_EMAILS` (comma-separated if you want more than one admin), then:
```bash
docker compose up -d
```
Log out and back in, and a **Domains** link appears in the top bar.

## Keeping it up to date

```bash
./update.sh
```

(`git pull` first if you want newer panel code.) Pulls the latest mail
server image, rebuilds the panel from whatever is checked out, and
restarts only what changed. Your mail, domains, mailboxes, and
DKIM keys all live in Docker volumes completely separate from the
container images, so an update never touches them.

## How it fits together

```
        the internet                          your server

  +----------------+                      +------------------------+
  | other mail     |    SMTP/IMAP/etc.    |      mailserver        |
  | servers, or    | -------------------> |  (docker-mailserver)   |
  | your phone's   | <------------------- |                        |
  | mail app       |    (public ports)    +-----------+------------+
  +----------------+                                  |
                                                      | shared config
                                                      | volume (files)
  +----------------+                      +-----------v------------+
  | your browser   |    reverse proxy     |         panel          |
  |                | -------------------> |    (Django + htmx)     |
  |                | <------------------- |   127.0.0.1:3010 only  |
  +----------------+  (you provide this)  +------------------------+
```

The panel is a plain IMAP/SMTP *client* for reading/sending mail (the same
protocols Thunderbird or your phone's mail app use, just talking to the
mail container over Docker's internal network) and manages domains/
mailboxes/DKIM by writing directly into a config directory the mail server
watches for changes — no API, no elevated permissions, no database shared
between the two. See **[docs/SETUP.md](docs/SETUP.md)** for the deep
reference (DNS records, firewall rules, the self-signed-certificate
tradeoff and how to use a real one, troubleshooting) and
`panel/webmail/mailadmin.py`'s module docstring if you're curious exactly
which files get written and why.

## Project layout

```
docker-compose.yml      the whole stack: mailserver + panel
setup.sh                generates .env, optionally walks you through the rest
update.sh               pulls/rebuilds and restarts, one command
.env.example            every setting, documented inline
panel/                  the Django + HTMX panel
  webmail/mail.py       IMAP/SMTP client (send/read mail)
  webmail/mailadmin.py  file-based admin layer (domains/mailboxes/DKIM)
  webmail/views.py      request handlers - start here to see how it fits together
docs/SETUP.md           full setup reference and troubleshooting
```

The code is commented fairly heavily on purpose, especially the trickier
parts (`mailadmin.py`'s module docstring, the reverse-proxy settings in
`panel/novella/settings.py`) — reading through it is a reasonable way to
understand how the whole thing works, not just how to run it.

## Security notes

- The panel is **not** meant to be exposed directly to the internet — it
  binds `127.0.0.1:3010` specifically so you put your own reverse proxy
  (with your own TLS certificate) in front of it.
- Mail protocol ports (25/465/587/143/993/...) **are** public, because
  that's what a mail server needs — nothing unusual there.
- `PERMIT_DOCKER=none` (in `docker-compose.yml`) keeps the mail server from
  being usable as an open relay by anything else on the host's Docker
  network. Don't change this unless you understand the tradeoff.
- Incoming HTML mail is sanitized (a strict tag/attribute allow-list) and
  rendered inside a sandboxed iframe as a second layer of defense — see
  `panel/webmail/mail.py`.
- The default TLS certificate is self-signed (see docs/SETUP.md section 7
  for switching to a real one) - fine for getting started, but mail
  clients will show a warning until you do.

## Contributing / license

Issues and pull requests are welcome. This repo doesn't currently ship a
`LICENSE` file — if you're the one deploying this publicly, pick one
(MIT and Apache-2.0 are common, permissive choices) before treating it as
fully open for others to reuse, since without one the default is "all
rights reserved."

## Credits

Built on [docker-mailserver](https://github.com/docker-mailserver/docker-mailserver),
[Django](https://www.djangoproject.com/), and [htmx](https://htmx.org).
