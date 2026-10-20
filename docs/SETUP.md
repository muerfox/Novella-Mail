# Setup

Two containers, brought up together with one `docker compose up -d` from
the repo root:

- **Mail engine** — [docker-mailserver](https://github.com/docker-mailserver/docker-mailserver):
  Postfix + Dovecot + Rspamd, a single prebuilt image (no vendored source -
  `docker-compose.yml` just references `ghcr.io/docker-mailserver/docker-mailserver`).
  Multi-domain by design: it derives its accepted domain list purely from
  whatever mailbox addresses exist, so hosting mail for unrelated domains
  needs no per-domain setup beyond adding their mailboxes.
- **Panel** (`panel/`) — a from-scratch Django + HTMX app: send/read/manage
  mail (a plain IMAP/SMTP client, like Thunderbird would be) plus a
  **Domains** section for allow-listed admin accounts to add domains,
  mailboxes, and DKIM keys. It manages docker-mailserver entirely by writing
  its config files directly into a shared volume — no REST API, no
  `docker exec`, no Docker socket access. docker-mailserver's own
  change-detector polls that volume every ~2s and reloads Postfix/Dovecot/
  Rspamd on its own.

Run everything in this doc **on the target server**, as a user in the `docker`
group (or with sudo).

## 1. Before you start

You need:

- A server with a public, static IP.
- A domain you control DNS for.
- Docker Engine >= 24 and the `docker compose` plugin >= 2.24.
- Port **25 outbound** not blocked by your hosting provider (many cloud
  providers block it by default for new accounts — check with them first,
  otherwise you can't send mail).

## 2. DNS records

There's a one-time server hostname to set up, then a small per-domain
checklist you repeat for every domain you add later (the panel's Domains
page shows you this same checklist, filled in, once a domain is added).

**Once, for the server itself** — pick a hostname for the mail server that
isn't necessarily one of the domains you'll host mail for, e.g.
`mail.example.com`:

| Type  | Name              | Value                                  |
|-------|-------------------|-----------------------------------------|
| A     | mail.example.com  | your server's public IP                |
| PTR   | (reverse DNS)     | mail.example.com — set via your hosting provider's control panel, not your DNS zone |

Getting PTR/rDNS right matters a lot — most receiving mail servers will
reject or spam-bin you without it.

**Per domain you add** (`yourdomain.tld`, could be totally unrelated to
`mail.example.com` above — that's the point):

| Type  | Name                          | Value                                  |
|-------|-------------------------------|-----------------------------------------|
| MX    | yourdomain.tld                | mail.example.com (priority 10)         |
| TXT   | yourdomain.tld                | `v=spf1 mx -all` (SPF)                 |
| TXT   | _dmarc.yourdomain.tld         | `v=DMARC1; p=quarantine; rua=mailto:postmaster@yourdomain.tld` |
| TXT   | mail._domainkey.yourdomain.tld| generated when you click "Generate DKIM key" on the domain's page in the panel |

You don't need to touch DNS for the mail server's own hostname again after
step one — every further domain only needs its own MX/SPF/DMARC/DKIM, all
pointing at the same `mail.example.com`.

## 3. Run the installer

```
git clone <this-repo-url>
cd Novella-Mail
./setup.sh
```

`setup.sh` copies `.env.example` to `.env`, asks for the mail server's
hostname and timezone, and fills in a random Django secret key. That's the
whole install step - no vendored source, no config generator, nothing else
to run.

Then bring everything up:

```
docker compose up -d
```

**No `.env` file?** That's fine too - every setting can come from the OS
environment instead (shell exports, a systemd unit, CI secrets), and a
value set there wins over the same key in `.env`. Only `MAIL_DOMAIN` and
`DJANGO_SECRET_KEY` are required; everything else falls back to the
defaults in `.env.example`:
```
export MAIL_DOMAIN=mail.example.com
export DJANGO_SECRET_KEY="$(head -c 512 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | cut -c1-50)"
export ADMIN_EMAILS=you@yourdomain.com
docker compose up -d
```
This needs Docker Compose 2.24 or newer (`docker compose version`). Extra
docker-mailserver settings (e.g. `ENABLE_CLAMAV`) only reach the mail
server through `.env`; without one, add them to the `mailserver`
service's `environment:` in `docker-compose.yml`.

First boot pulls the docker-mailserver image and builds the panel image -
needs internet access for that, the only point in the whole setup that
does. First-time container init (Postfix/Dovecot bootstrapping) takes a
minute or so. Watch it with `docker compose logs -f`.

**"Pool overlaps with other one on this address space"** — if you hit this
on `docker compose up -d`, some other docker network on this host already
uses the subnet Compose auto-picked for this project's internal network.
Check what's taken:
```
docker network inspect $(docker network ls -q) -f '{{.Name}}: {{range .IPAM.Config}}{{.Subnet}} {{end}}'
```
then add a `networks:` block to `docker-compose.yml` pinning an unused
subnet, e.g.:
```yaml
networks:
  default:
    ipam:
      config:
        - subnet: 10.201.77.0/24
```

## 4. Firewall

Open only the mail ports publicly. Do **not** open the panel's port
(3010 by default).

```
sudo ufw allow 25/tcp    # SMTP (inbound mail)
sudo ufw allow 465/tcp   # SMTPS (submission, implicit TLS)
sudo ufw allow 587/tcp   # Submission (STARTTLS)
sudo ufw allow 143/tcp   # IMAP (STARTTLS)
sudo ufw allow 993/tcp   # IMAPS
sudo ufw allow 110/tcp   # POP3 (STARTTLS, optional)
sudo ufw allow 995/tcp   # POP3S (optional)
```

## 5. Reaching the panel

The panel only listens on `127.0.0.1:3010`. This repo assumes you're
putting your own reverse proxy (nginx/Caddy/Traefik/whatever) in front of
it for public HTTPS access - point it at `127.0.0.1:3010` and make sure it
sets `X-Forwarded-Proto` (see `novella/settings.py`'s reverse-proxy
section; also set `CSRF_TRUSTED_ORIGINS` in `.env` to the public URL you
serve it under).

No reverse proxy yet? An SSH tunnel works for yourself in the meantime:
```
ssh -L 3010:127.0.0.1:3010 you@your-server
```
then open `http://127.0.0.1:3010`.

## 6. First login

There's no separate admin UI to bootstrap from - create the first mailbox
directly:

```
docker compose exec panel python manage.py createmailbox you@yourdomain.com
```

(Prompts for a password; add `--password` to skip the prompt, or `--quota`
to override the default 3G.) Then log into the panel with that email and
password — it authenticates by trying a real IMAP login with whatever you
type, there's no separate panel account.

**Unlock the Domains section** for that account:
1. Edit `.env`, set `ADMIN_EMAILS=` to that email (comma separate more if
   needed).
2. `docker compose up -d` (only the `panel` service restarts).
3. Log out and back into the panel — a "Domains" link now shows in the top
   bar.

## 7. TLS for mail (IMAPS/SMTPS) — the tradeoff

`.env` defaults `SSL_TYPE=self-signed`, so mail services get a self-signed
cert with no setup required - that's also why `MAIL_TLS_INSECURE=true` is
the default (it disables certificate verification on the panel's own
internal IMAP/SMTP connections to docker-mailserver). Desktop/mobile mail
clients will show an "untrusted certificate" warning on first connect.

To use a real certificate instead, get one however you'd like (e.g. `certbot`
with a DNS-01 plugin, run separately - docker-mailserver doesn't obtain
certificates itself, it only reads ones you provide), then in `.env`:
```
SSL_TYPE=manual
SSL_CERT_PATH=/path/inside/the/mailserver/container/to/fullchain.pem
SSL_KEY_PATH=/path/inside/the/mailserver/container/to/privkey.pem
```
mounting your real cert directory into the `mailserver` service in
`docker-compose.yml` so those paths resolve, then set
`MAIL_TLS_INSECURE=false` in `.env` and `docker compose up -d`.

## 8. Updating

```
docker compose pull mailserver
docker compose build panel
docker compose up -d
```

Domain/mailbox/DKIM data lives in the `mailserver-config` docker volume,
independent of the image version - updating the image doesn't touch it.

## 9. Adding more domains (self-service, entirely from the panel)

Once `ADMIN_EMAILS` is set up (step 6), any further domain — for a client,
a side project, whatever — goes entirely through the panel:

1. Log into the panel, open **Domains** → type the domain → **Go to
   domain**. There's no separate "create" step: docker-mailserver derives
   its domain list from whatever mailbox addresses exist, so a domain
   "exists" the moment you add DKIM or a mailbox for it.
2. Its page shows the exact DNS records to publish (MX/SPF/DMARC, matching
   the checklist in section 2). Add them at whatever DNS provider that
   domain uses — it doesn't need to be anywhere near where `mail.example.com`
   is hosted.
3. Click **Generate DKIM key** and publish the TXT record it gives you.
4. Add a mailbox for the domain (address + password).
5. Once DNS propagates (MX especially — check with `dig MX yourdomain.tld`),
   mail for that domain is live — within a few seconds of adding the
   mailbox, no container restart needed.

Deleting a domain from its page removes it and all its mailboxes — there's
a confirmation prompt since it's not reversible. (It doesn't delete the
actual mail data under the `mail-data` volume or a generated DKIM private
key file, just the account/alias/signing-config entries - safe to leave;
ask if you want a "purge everything" option added.)

## 10. Clients, quotas and suspension

**Clients** (top bar → Clients) are the customers you host mail for. Each
client groups one or more domains and has three limits you set and can
change anytime: max domains, max mailboxes, and total storage. The panel
refuses any change that would go over a limit - adding a domain or
mailbox, or raising a quota. Clients don't get admin access; they only log
into webmail with their mailbox, which shows a storage bar under the
folder list and a Password link to change their own password. Domains
not assigned to a client have no limits.

- **Quotas** - set per mailbox when creating it, and change it later from
  the domain's mailbox table (`500M`, `3G`, `1T`). A client's quotas
  together can't exceed its total storage.
- **Usage** - the mailbox table and client pages show approximate disk
  use, read from the `mail-data` volume (mounted read-only into the panel).
- **Suspension** - suspend a single mailbox from its domain page, or a
  whole client from its page. Suspended mailboxes can't log in to webmail,
  IMAP/POP3 or send over SMTP; incoming mail is still accepted. Admin
  mailboxes (`ADMIN_EMAILS`) are never suspended.

Clients and suspensions are stored in `novella-clients.json` in the
`mailserver-config` volume, next to docker-mailserver's own files.

## 11. What the panel can and can't do yet

Covered: read/search/star/mark read/delete/reply/forward/compose with
attachments, mailbox owners changing their own password (Password link in
the top bar), domain + mailbox + DKIM administration (section 9), and
clients with limits, quotas and suspension (section 10). Not yet covered:
aliases/forwarders, catch-all addresses, admin-forced password resets,
spam-filter tuning. Say the word if you want any of those added.
