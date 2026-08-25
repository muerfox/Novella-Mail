#!/usr/bin/env bash
#
# Novella-Mail setup:
#  1. Fetches mailcow-dockerized (Postfix/Dovecot/Rspamd/ClamAV) as the mail
#     engine, with SOGo webmail disabled and its own admin UI moved to a
#     second localhost-only port (3006) - domains/DKIM/quotas/spam settings
#     live there until the custom panel covers them too.
#  2. Generates panel/.env for the custom Django+HTMX panel, which becomes
#     the primary panel on 127.0.0.1:3007.
#
# Run this ON THE TARGET SERVER, not on your dev machine.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${ROOT_DIR}/mailcow-dockerized"
PANEL_DIR="${ROOT_DIR}/panel"

MAILCOW_UI_PORT="${MAILCOW_UI_PORT:-3006}"
MAILCOW_UI_HTTPS_PORT="${MAILCOW_UI_HTTPS_PORT:-3008}"
PANEL_PORT="${PANEL_PORT:-3007}"
PANEL_BIND="${PANEL_BIND:-127.0.0.1}"

echo "==> Checking prerequisites"
command -v docker >/dev/null || { echo "docker is not installed. See https://docs.docker.com/engine/install/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin is not available."; exit 1; }

if [ -d "$STACK_DIR/.git" ]; then
  echo "==> mailcow-dockerized already present, pulling latest"
  git -C "$STACK_DIR" pull
else
  echo "==> Cloning mailcow-dockerized"
  git clone https://github.com/mailcow/mailcow-dockerized.git "$STACK_DIR"
fi

cd "$STACK_DIR"
ln -sf mailcow.conf .env

echo "==> Running upstream config generator (you will be asked for the mail server's FQDN, e.g. mail.yourdomain.com, and timezone)"
./generate_config.sh

MAILCOW_API_KEY="$(LC_ALL=C </dev/urandom tr -dc 'A-Za-z0-9' 2>/dev/null | head -c 40)"
MAILCOW_HOSTNAME="$(grep -E '^MAILCOW_HOSTNAME=' mailcow.conf | cut -d= -f2-)"
IPV4_NETWORK="$(grep -E '^IPV4_NETWORK=' mailcow.conf | cut -d= -f2-)"
COMPOSE_PROJECT_NAME="$(grep -E '^COMPOSE_PROJECT_NAME=' mailcow.conf | cut -d= -f2-)"
MAILCOW_NETWORK_NAME="${COMPOSE_PROJECT_NAME}_mailcow-network"

echo "==> Patching mailcow.conf: mailcow's own UI moved to ${PANEL_BIND}:${MAILCOW_UI_PORT}, SOGo webmail disabled, mail ports left public, API enabled for the panel"
sed -i \
  -e "s/^HTTP_PORT=.*/HTTP_PORT=${MAILCOW_UI_PORT}/" \
  -e "s/^HTTP_BIND=.*/HTTP_BIND=${PANEL_BIND}/" \
  -e "s/^HTTPS_PORT=.*/HTTPS_PORT=${MAILCOW_UI_HTTPS_PORT}/" \
  -e "s/^HTTPS_BIND=.*/HTTPS_BIND=${PANEL_BIND}/" \
  -e "s/^HTTP_REDIRECT=.*/HTTP_REDIRECT=n/" \
  -e "s/^SKIP_LETS_ENCRYPT=.*/SKIP_LETS_ENCRYPT=y/" \
  -e "s/^SKIP_SOGO=.*/SKIP_SOGO=y/" \
  -e "s/^#*API_KEY=.*/API_KEY=${MAILCOW_API_KEY}/" \
  -e "s/^#*API_ALLOW_FROM=.*/API_ALLOW_FROM=127.0.0.1,${IPV4_NETWORK}.0\/24/" \
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
    -e "s/^MAILCOW_NETWORK_NAME=.*/MAILCOW_NETWORK_NAME=${MAILCOW_NETWORK_NAME}/" \
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

Next steps:
  cd mailcow-dockerized && docker compose pull && docker compose up -d
  cd ../panel && docker compose build && docker compose up -d

Then, to unlock self-service domain adding in the panel ("Domains" section):
  1. Log into the mailcow admin UI (http://${PANEL_BIND}:${MAILCOW_UI_PORT}, admin/moohoo,
     change the password) and create your first domain + mailbox.
  2. Log into the Novella panel with that mailbox's email + password.
  3. Add that same email to ADMIN_EMAILS in panel/.env, then:
     cd panel && docker compose up -d
  From then on, any further domain just needs its MX pointed at
  ${MAILCOW_HOSTNAME:-your mail server} and adding via the panel's Domains page - no
  more manual mailcow-admin-UI steps.

Read docs/SETUP.md for DNS records, firewall rules, first login, and how to
get a trusted TLS cert instead of the self-signed one.
EOF
