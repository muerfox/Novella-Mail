# Setup

Two pieces, both in Docker, brought up together with one `docker compose up
-d` from the repo root (see `docker-compose.yml`, which just `include:`s
each piece's own compose file into a single project):

- **Mail engine** — [mailcow-dockerized](https://github.com/mailcow/mailcow-dockerized):
  Postfix + Dovecot + Rspamd + ClamAV. Vendored in this repo as a git
  submodule (`mailcow-dockerized/`, pinned to a release tag) — cloning this
  repo with `--recurse-submodules` gets you the whole thing, no separate
  fetch at deploy time. It's multi-domain by design — one mail server can
  host any number of unrelated domains, each with its own mailboxes and
  DKIM key. SOGo webmail is disabled; mailcow's own admin UI stays up as a
  secondary, localhost-only panel for things the custom panel doesn't cover
  yet (quotas, spam rules, ClamAV).
- **Panel** (`panel/`) — a custom Django + HTMX app that's the primary,
  everyday UI: send/read/manage mail (a plain IMAP/SMTP client, like
  Thunderbird would be) plus a **Domains** section for adding domains and
  mailboxes yourself, calling mailcow's admin API over the internal docker
  network. Add a domain, point its MX here, and the mail engine serves mail
  for it immediately — no restart, no per-domain container.

Run everything in this doc **on the target server**, as a user in the `docker`
group (or with sudo).

## 1. Before you start

You need:

- A server with a public, static IP.
- A domain you control DNS for.
- Docker Engine >= 24 and the `docker compose` plugin (>= 2.20, for the
  `include:` the root `docker-compose.yml` uses) installed on the server.
- Port **25 outbound** not blocked by your hosting provider (many cloud
  providers block it by default for new accounts — check with them first,
  otherwise you can't send mail).

Get the repo onto the server with submodules included in one shot:

```
git clone --recurse-submodules <this-repo-url>
```

(If you already cloned without `--recurse-submodules`, `./setup.sh` fetches
it for you, or run `git submodule update --init --recursive` yourself.)

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
| TXT   | dkim._domainkey.yourdomain.tld| generated when you click "Generate DKIM key" on the domain's page in the panel |

You don't need to touch DNS for the mail server's own hostname again after
step one — every further domain only needs its own MX/SPF/DMARC/DKIM, all
pointing at the same `mail.example.com`.

## 3. Run the installer

```
./setup.sh
```

This:

1. Makes sure the `mailcow-dockerized` submodule is checked out (fetches it
   if you skipped `--recurse-submodules`), then runs its official
   `generate_config.sh` (asks for `mail.example.com` as the hostname, and
   your timezone) — this needs the submodule's own `.git`, which is exactly
   why it's vendored as a real submodule and not plain copied-in files.
2. Patches `mailcow-dockerized/mailcow.conf` so:
   - Mailcow's own web UI binds to `127.0.0.1:3006` (and `:3008` for https)
     — **not reachable from the internet**.
   - `SKIP_SOGO=y` — SOGo webmail is off, since the Django panel replaces it.
   - Mail protocol ports (25, 465, 587, 143, 993, 110, 995, 4190) stay bound
     on all interfaces, as required for a working mail server.
   - A random read-write API key is set (`API_KEY`), restricted
     (`API_ALLOW_FROM`) to localhost and the internal docker subnet only —
     this is what lets the panel manage domains/mailboxes.
3. Generates `panel/.env` from `panel/.env.example`, filling in a random
   `DJANGO_SECRET_KEY`, pointing `MAIL_IMAP_HOST`/`MAIL_SMTP_HOST` at your
   `MAILCOW_HOSTNAME`, and wiring up `MAILCOW_API_KEY`/`MAILCOW_API_BASE_URL`
   so the panel can reach mailcow's API.

Then bring everything up with one command from the **repo root**:

```
docker compose up -d
```

The root `docker-compose.yml` just `include:`s `mailcow-dockerized/docker-
compose.yml` and `panel/docker-compose.yml` into a single project, so this
one command builds the panel image and pulls every mailcow image, creates
the shared internal network, and starts all of it together. First boot
takes a few minutes (DB init, etc). Watch it with `docker compose logs -f`.

That first `up -d` needs internet access to pull/build images (there's no
practical way to vendor multi-gigabyte container images into a git repo).
Everything else about this repo — the mail engine's config/source, the
panel's own code, and the compose wiring — is self-contained and needs no
further network access to bring the stack up or down again afterwards.

**"Pool overlaps with other one on this address space"**: `setup.sh` puts
mailcow's internal docker network on `10.89.1.0/24` specifically to dodge
the `172.16.0.0/12` range most Docker hosts auto-allocate project networks
from (mailcow's own default, `172.22.1.0/24`, sits right inside that range
and collides constantly on a box already running other docker-compose
projects). If `10.89.1.0/24` *also* collides, check what's already taken:
```
docker network inspect $(docker network ls -q) -f '{{.Name}}: {{range .IPAM.Config}}{{.Subnet}} {{end}}'
```
then either re-run `MAILCOW_IPV4_NETWORK=x.x.x ./setup.sh` before first
bringing the stack up, or fix it in place on an existing install:
```
sed -i \
  -e "s/^IPV4_NETWORK=.*/IPV4_NETWORK=x.x.x/" \
  -e "s/^API_ALLOW_FROM=.*/API_ALLOW_FROM=127.0.0.1,x.x.x.0\/24/" \
  mailcow-dockerized/mailcow.conf
docker compose up -d
```

## 4. Firewall

Open only the mail ports publicly. Do **not** open 3006, 3008, or 3009.

```
sudo ufw allow 25/tcp    # SMTP (inbound mail)
sudo ufw allow 465/tcp   # SMTPS (submission, implicit TLS)
sudo ufw allow 587/tcp   # Submission (STARTTLS)
sudo ufw allow 143/tcp   # IMAP (STARTTLS)
sudo ufw allow 993/tcp   # IMAPS
sudo ufw allow 995/tcp   # POP3S (optional, if you use POP3)
sudo ufw allow 4190/tcp  # Sieve (optional, for client-side mail filters)
```

## 5. Reaching the panels

Both panels only listen on localhost. Reach them one of two ways:

**SSH tunnel (simplest, no extra setup):**

```
ssh -L 3009:127.0.0.1:3009 -L 3006:127.0.0.1:3006 you@your-server
```

then open `http://127.0.0.1:3009` (Novella panel, day-to-day use) or
`http://127.0.0.1:3006` (mailcow admin UI, for DKIM/quotas/spam settings) in
your local browser.

**Reverse proxy with your own TLS** (if you want a public
`https://mail.example.com` URL with a real cert): put nginx/Caddy/Traefik on
the host in front of `127.0.0.1:3009`, terminating TLS there. This repo
doesn't set that up for you since it depends on how you want auth/TLS
handled — ask if you want a Caddy config added.

## 6. First login and bootstrapping domain admin

The very first domain + mailbox has to be created through the mailcow admin
UI, since the panel's own Domains section needs a mailbox to log in with
before it can do anything. After that, every further domain goes through the
panel.

**Mailcow admin UI** (`http://127.0.0.1:3006`, via the tunnel above):
- User: `admin` / Password: `moohoo`
- **Change this password immediately** (Access → Admin Details), then add
  your first mail domain (Configuration → Mail Setup → Domains) and create
  at least one mailbox — that mailbox's email + password is what you log
  into the Novella panel with, there's no separate account system there.

**Novella panel** (`http://127.0.0.1:3009`):
- Log in with the email + password of the mailbox you just created. The
  panel authenticates by trying an IMAP login with whatever you type —
  there's no separate panel account to set up.

**Unlock the Domains section** for that account:
1. Edit `panel/.env`, set `ADMIN_EMAILS=` to that mailbox's email (comma
   separate more if needed).
2. From the repo root: `docker compose up -d` (only the `panel` service
   restarts; mailcow is untouched).
3. Log out and back into the panel — a "Domains" link now shows in the
   top bar.

## 7. TLS for mail (IMAPS/SMTPS) — the tradeoff

Because neither panel is publicly reachable on 80/443, `setup.sh` sets
`SKIP_LETS_ENCRYPT=y` in mailcow.conf. Mail services (and the IMAP/SMTP
connections the Django panel itself makes) get a self-signed cert — that's
why `panel/.env` is generated with `MAIL_TLS_INSECURE=true`, which disables
TLS certificate verification on those connections. Desktop/mobile mail
clients will show an "untrusted certificate" warning on first connect too.
Two ways to get a real cert without exposing anything publicly long-term:

**Option A — DNS-01 challenge (recommended, no public port needed).**
Edit `mailcow-dockerized/mailcow.conf`:
```
SKIP_LETS_ENCRYPT=n
ACME_DNS_CHALLENGE=y
ACME_DNS_PROVIDER=dns_xxx   # your DNS provider's acme.sh plugin name
ACME_ACCOUNT_EMAIL=you@example.com
```
plus the provider-specific API credentials as env vars (see the comments in
`mailcow.conf` and https://docs.mailcow.email for your DNS provider). Then,
from the repo root, `docker compose up -d` to restart the ACME container.
Once it's issued a real cert, set `MAIL_TLS_INSECURE=false` in `panel/.env`
and `docker compose up -d` again to pick it up.

**Option B — temporarily expose 80/443.** Open those ports publicly, set
`HTTP_BIND=`, `HTTPS_BIND=`, `SKIP_LETS_ENCRYPT=n`, bring the stack up so
acme-mailcow can issue/renew, then close the ports again. Works, but you'll
need to repeat it every ~60 days for renewal, so Option A is the better fit
for a permanently-locked-down setup.

## 8. Updating

Mail engine — since it's a real git checkout (just vendored as a submodule
instead of fetched fresh), mailcow's own update flow works as documented:
```
cd mailcow-dockerized
git pull
./update.sh
```
Never hand-edit files under `mailcow-dockerized/data/` — `update.sh`
overwrites them. Afterwards, from the repo root, commit the submodule's new
pinned commit so it's recorded in this repo's history:
```
git add mailcow-dockerized
git commit -m "Update mailcow-dockerized"
```

Panel:
```
docker compose build panel
docker compose up -d
```

## 9. Adding more domains (self-service, no mailcow admin UI needed)

Once `ADMIN_EMAILS` is set up (step 6), any further domain — for a client,
a side project, whatever — goes entirely through the panel:

1. Log into the panel, open **Domains** → **Add a domain**, enter the
   domain and an optional description.
2. Its page shows the exact DNS records to publish (MX/SPF/DMARC, matching
   the checklist in section 2). Add them at whatever DNS provider that
   domain uses — it doesn't need to be anywhere near where `mail.example.com`
   is hosted.
3. Click **Generate DKIM key** and publish the TXT record it gives you.
4. Add a mailbox for the domain (address, display name, password).
5. Once DNS propagates (MX especially — check with `dig MX yourdomain.tld`),
   mail for that domain is live. No container restart, no redeploy — mailcow
   picks up new domains from its database immediately.

Deleting a domain from its page removes it and all its mailboxes from
mailcow — there's a confirmation prompt since it's not reversible.

## 10. What the panel can and can't do yet

Covered: read/search/star/mark read/delete/reply/forward/compose with
attachments, and domain + mailbox + DKIM administration (section 9). Not yet
covered: per-mailbox quota tuning, aliases, spam-filter rules, ClamAV
settings — use the mailcow admin UI (`127.0.0.1:3006`) for those.
