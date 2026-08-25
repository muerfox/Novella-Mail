#!/usr/bin/env bash
#
# Novella-Mail setup:
#  1. Makes sure mailcow-dockerized (Postfix/Dovecot/Rspamd/ClamAV, vendored
#     as a git submodule - no network fetch needed at setup time) is
#     checked out, with SOGo webmail disabled and its own admin UI moved to
#     a second localhost-only port (3006) - domains/DKIM/quotas/spam
#     settings live there until the custom panel covers them too.
#  2. Generates panel/.env for the custom Django+HTMX panel, which becomes
#     the primary panel on 127.0.0.1:3009.
#
# Everything then comes up with one command from the repo root:
#   docker compose up -d
#
# Run this ON THE TARGET SERVER, not on your dev machine.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${ROOT_DIR}/mailcow-dockerized"
PANEL_DIR="${ROOT_DIR}/panel"

MAILCOW_UI_PORT="${MAILCOW_UI_PORT:-3006}"
MAILCOW_UI_HTTPS_PORT="${MAILCOW_UI_HTTPS_PORT:-3008}"
PANEL_PORT="${PANEL_PORT:-3009}"
PANEL_BIND="${PANEL_BIND:-127.0.0.1}"
# mailcow's own default (172.22.1.0/24) sits inside the 172.16.0.0/12 block
# most Docker hosts auto-allocate project networks from, so it collides
# constantly on a box that already runs several docker-compose projects.
# 10.x is far less likely to already be in use. Override with
# MAILCOW_IPV4_NETWORK=x.x.x ./setup.sh if this ALSO collides (check with
# `docker network inspect $(docker network ls -q) -f '{{.Name}}: {{range .IPAM.Config}}{{.Subnet}} {{end}}'`).
MAILCOW_IPV4_NETWORK="${MAILCOW_IPV4_NETWORK:-10.89.1}"

echo "==> Checking prerequisites"
command -v docker >/dev/null || { echo "docker is not installed. See https://docs.docker.com/engine/install/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin is not available."; exit 1; }

if [ ! -f "${STACK_DIR}/docker-compose.yml" ]; then
  echo "==> mailcow-dockerized submodule not checked out yet, fetching it"
  git -C "$ROOT_DIR" submodule update --init --recursive
fi

cd "$STACK_DIR"
ln -sf mailcow.conf .env

echo "==> Running upstream config generator (you will be asked for the mail server's FQDN, e.g. mail.yourdomain.com, and timezone)"
echo "    It writes mailcow.conf right away, then tails off into a purely cosmetic"
echo "    git version-stamp step (for the mailcow UI footer) that can occasionally"
echo "    stall after 'Copying snake-oil certificate...' for reasons unrelated to"
echo "    this repo (git upstream-tracking quirks). If it sits there for more than"
echo "    a minute or two after that line, Ctrl+C is safe - mailcow.conf is already"
echo "    written by then, and this script continues from here regardless."
set +e
./generate_config.sh
gc_status=$?
set -e
if [ "$gc_status" -ne 0 ]; then
  if [ -s mailcow.conf ]; then
    echo "==> generate_config.sh didn't exit cleanly (status ${gc_status}) but mailcow.conf was written - continuing."
  else
    echo "generate_config.sh failed before writing mailcow.conf. Re-run ./setup.sh." >&2
    exit 1
  fi
fi

MAILCOW_API_KEY="$(LC_ALL=C </dev/urandom tr -dc 'A-Za-z0-9' 2>/dev/null | head -c 40)"
MAILCOW_HOSTNAME="$(grep -E '^MAILCOW_HOSTNAME=' mailcow.conf | cut -d= -f2-)"

echo "==> Patching mailcow.conf: mailcow's own UI moved to ${PANEL_BIND}:${MAILCOW_UI_PORT}, SOGo webmail disabled, mail ports left public, API enabled for the panel, internal subnet moved to ${MAILCOW_IPV4_NETWORK}.0/24"
sed -i \
  -e "s/^HTTP_PORT=.*/HTTP_PORT=${MAILCOW_UI_PORT}/" \
  -e "s/^HTTP_BIND=.*/HTTP_BIND=${PANEL_BIND}/" \
  -e "s/^HTTPS_PORT=.*/HTTPS_PORT=${MAILCOW_UI_HTTPS_PORT}/" \
  -e "s/^HTTPS_BIND=.*/HTTPS_BIND=${PANEL_BIND}/" \
  -e "s/^HTTP_REDIRECT=.*/HTTP_REDIRECT=n/" \
  -e "s/^SKIP_LETS_ENCRYPT=.*/SKIP_LETS_ENCRYPT=y/" \
  -e "s/^SKIP_SOGO=.*/SKIP_SOGO=y/" \
  -e "s/^#*API_KEY=.*/API_KEY=${MAILCOW_API_KEY}/" \
  -e "s/^#*API_ALLOW_FROM=.*/API_ALLOW_FROM=127.0.0.1,${MAILCOW_IPV4_NETWORK}.0\/24/" \
  -e "s/^IPV4_NETWORK=.*/IPV4_NETWORK=${MAILCOW_IPV4_NETWORK}/" \
  mailcow.conf

echo "==> Generating panel/.env"
cd "$PANEL_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
  SECRET_KEY="$(LC_ALL=C </dev/urandom tr -dc 'A-Za-z0-9' 2>/dev/null | head -c 50)"
  sed -i \
    -e "s/^DJANGO_SECRET_KEY=.*/DJANGO_SECRET_KEY=${SECRET_KEY}/" \
    -e "s/^DJANGO_DEBUG=.*/DJANGO_DEBUG=false/" \
    -e "s/^DJANGO_ALLOWED_HOSTS=.*/DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,${MAILCOW_HOSTNAME}/" \
    -e "s/^MAIL_IMAP_HOST=.*/MAIL_IMAP_HOST=${MAILCOW_HOSTNAME}/" \
    -e "s/^MAIL_SMTP_HOST=.*/MAIL_SMTP_HOST=${MAILCOW_HOSTNAME}/" \
    -e "s/^MAIL_TLS_INSECURE=.*/MAIL_TLS_INSECURE=true/" \
    -e "s/^MAILCOW_API_BASE_URL=.*/MAILCOW_API_BASE_URL=http:\/\/nginx:${MAILCOW_UI_PORT}/" \
    -e "s/^MAILCOW_API_KEY=.*/MAILCOW_API_KEY=${MAILCOW_API_KEY}/" \
    -e "s/^MAILCOW_HOSTNAME=.*/MAILCOW_HOSTNAME=${MAILCOW_HOSTNAME}/" \
    -e "s/^PANEL_PORT=.*/PANEL_PORT=${PANEL_PORT}/" \
    .env
  echo "    Wrote panel/.env (MAIL_TLS_INSECURE=true because SKIP_LETS_ENCRYPT=y gives a"
  echo "    self-signed cert - flip it to false once you've set up a trusted cert, see"
  echo "    docs/SETUP.md section 7)."
else
  echo "    panel/.env already exists, leaving it alone."
fi

cat <<EOF

==> Done.

Mail engine:     mailcow-dockerized/mailcow.conf
Mailcow admin UI: http://${PANEL_BIND}:${MAILCOW_UI_PORT}  (domains, DKIM, quotas, spam - SOGo webmail disabled)
Novella panel:    http://${PANEL_BIND}:${PANEL_PORT}  (the custom Django+HTMX send/manage-mail panel)
Mail ports:       SMTP 25, SMTPS 465, Submission 587, IMAP 143, IMAPS 993, POP3 110/995, Sieve 4190 - all public.
Internal subnet:  ${MAILCOW_IPV4_NETWORK}.0/24 (mailcow's docker network - if "docker compose up -d"
                  fails with a "Pool overlaps" error, some other docker network on this host
                  already uses that range; re-run with MAILCOW_IPV4_NETWORK=x.x.x ./setup.sh
                  after checking with docker network ls / docker network inspect - see
                  docs/SETUP.md for the exact command).

Next step - one command from the repo root brings up everything:
  docker compose up -d

(The first run pulls/builds every image - it needs internet for that one
time. After the images are local, bringing the stack up/down again doesn't.)

Then, to unlock self-service domain adding in the panel ("Domains" section):
  1. Log into the mailcow admin UI (http://${PANEL_BIND}:${MAILCOW_UI_PORT}, admin/moohoo,
     change the password) and create your first domain + mailbox.
  2. Log into the Novella panel with that mailbox's email + password.
  3. Add that same email to ADMIN_EMAILS in panel/.env, then:
     docker compose up -d
  From then on, any further domain just needs its MX pointed at
  ${MAILCOW_HOSTNAME:-your mail server} and adding via the panel's Domains page - no
  more manual mailcow-admin-UI steps.

Read docs/SETUP.md for DNS records, firewall rules, first login, and how to
get a trusted TLS cert instead of the self-signed one.
EOF
