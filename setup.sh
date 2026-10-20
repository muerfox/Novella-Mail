#!/usr/bin/env bash
#
# setup.sh - the entire install step for Novella-Mail.
#
# All it really does is create a `.env` file (copied from `.env.example`,
# with a few values filled in) and, if you want, walk you through bringing
# the stack up and creating your first mailbox. There's no separate config
# generator to run and nothing to vendor: the mail server
# (docker-mailserver) is a single prebuilt Docker image, and the panel
# manages it entirely by writing into a shared config volume.
#
# Safe to re-run: if `.env` already exists, this script won't touch it - it
# only asks questions and generates secrets the very first time.
#
# Run this ON THE SERVER where you actually want mail hosted, not on your
# laptop/dev machine (unless that's the same thing).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# --- small helpers -------------------------------------------------------

# Ask a yes/no question with a default. Usage: ask_yes_no "Question?" Y
ask_yes_no() {
  local question="$1" default="${2:-Y}" reply
  local hint="[Y/n]"
  [ "$default" = "N" ] && hint="[y/N]"
  read -r -p "${question} ${hint} " reply
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy] ]]
}

# Very loose FQDN sanity check: needs at least one dot and no whitespace.
# Not trying to be a strict RFC validator here - just catch obvious typos
# ("mailexamplecom", a pasted URL with a scheme, stray spaces) before they
# end up baked into .env.
looks_like_a_domain() {
  [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$ ]]
}

# --- 1. prerequisites ------------------------------------------------------

echo "==> Checking prerequisites"
command -v docker >/dev/null || { echo "docker is not installed. See https://docs.docker.com/engine/install/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin is not available."; exit 1; }

# --- 2. generate .env (only if it doesn't exist yet) ----------------------

if [ -f .env ]; then
  echo "==> .env already exists, leaving it alone."
else
  cp .env.example .env

  # Keep asking until we get something that at least looks like a domain.
  # Empty input always loops; a domain-shaped answer is accepted straight
  # away; anything else gets a warning with the option to use it anyway
  # (in case the check is wrong for someone's edge case).
  MAIL_DOMAIN=""
  while [ -z "$MAIL_DOMAIN" ]; do
    read -r -p "Mail server hostname (FQDN, e.g. mail.yourdomain.com - this is the server's own identity, not a domain you're hosting mail for): " CANDIDATE
    if [ -z "$CANDIDATE" ]; then
      continue
    elif looks_like_a_domain "$CANDIDATE"; then
      MAIL_DOMAIN="$CANDIDATE"
    elif ask_yes_no "'${CANDIDATE}' doesn't look like a normal hostname - use it anyway?" N; then
      MAIL_DOMAIN="$CANDIDATE"
    fi
  done

  # Debian/Ubuntu keep the system timezone name in /etc/timezone (e.g.
  # "Europe/Berlin") - offer that as the default so most people can just
  # hit enter.
  DETECTED_TZ="UTC"
  if [ -f /etc/timezone ]; then
    DETECTED_TZ="$(cat /etc/timezone)"
  fi
  read -r -p "Timezone [${DETECTED_TZ}]: " TZ_INPUT
  TZ_VALUE="${TZ_INPUT:-$DETECTED_TZ}"

  # Generate a random Django secret key. NOTE on the pipeline shape below:
  # bounding the /dev/urandom read to a fixed size *before* filtering, then
  # truncating in bash, is deliberate. The tempting one-liner
  #   </dev/urandom tr -dc 'A-Za-z0-9' | head -c 50
  # pipes an *unbounded* stream through `tr` into `head -c 50`: as soon as
  # head has its 50 bytes it closes its end of the pipe, and tr - which is
  # still trying to write more, since urandom never runs out - can get
  # SIGPIPE'd for writing to a reader that already left. Under `set -e -o
  # pipefail` that intermittently (it's a timing race, so it doesn't fail
  # every time - which makes it a nasty one to debug) kills the whole
  # script with no useful error message. Reading a fixed amount first sidesteps
  # the race entirely: `head -c 512 /dev/urandom` reads exactly 512 bytes
  # and stops on its own, nothing is still trying to write afterwards.
  RAW_RANDOM="$(head -c 512 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9')"
  SECRET_KEY="${RAW_RANDOM:0:50}"

  # `#` as the sed delimiter for the TZ line specifically, since timezone
  # names contain forward slashes (Europe/Berlin, America/New_York, ...)
  # which would otherwise collide with sed's default `/` delimiter and
  # break the expression.
  sed -i \
    -e "s/^MAIL_DOMAIN=.*/MAIL_DOMAIN=${MAIL_DOMAIN}/" \
    -e "s#^TZ=.*#TZ=${TZ_VALUE}#" \
    -e "s/^DJANGO_SECRET_KEY=.*/DJANGO_SECRET_KEY=${SECRET_KEY}/" \
    -e "s/^DJANGO_ALLOWED_HOSTS=.*/DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,${MAIL_DOMAIN}/" \
    .env

  echo "==> Wrote .env for ${MAIL_DOMAIN}"
fi

# --- 3. optionally do the rest for you -------------------------------------
# Everything past this point is optional hand-holding: bring the stack up,
# create the first mailbox, and wire it into ADMIN_EMAILS, all without
# having to look up and copy/paste the commands yourself. Say no to any of
# these and docs/SETUP.md has the exact manual commands instead.

MAILBOX_CREATED=""

echo
if ask_yes_no "Bring the stack up now (docker compose up -d)? First run pulls images, needs internet." Y; then
  docker compose up -d

  echo
  if ask_yes_no "Create your first mailbox now? (the panel needs one to log in with)" Y; then
    read -r -p "Email address for the first mailbox (e.g. you@yourdomain.com): " FIRST_EMAIL
    # createmailbox prompts for the password itself (hidden input) - not
    # passed as a --password flag here on purpose, so it never ends up in
    # this script's process list or shell history.
    if docker compose exec panel python manage.py createmailbox "$FIRST_EMAIL"; then
      MAILBOX_CREATED="$FIRST_EMAIL"
      echo
      if ask_yes_no "Add ${FIRST_EMAIL} to ADMIN_EMAILS so it can manage domains from the panel's Domains section?" Y; then
        if grep -q "^ADMIN_EMAILS=$" .env; then
          sed -i "s/^ADMIN_EMAILS=.*/ADMIN_EMAILS=${FIRST_EMAIL}/" .env
        else
          # ADMIN_EMAILS already has something in it (re-run of setup, or
          # hand-edited) - append rather than clobber whoever's there.
          sed -i "s/^ADMIN_EMAILS=.*/&,${FIRST_EMAIL}/" .env
        fi
        docker compose up -d
        echo "==> Done - log in at the panel and you'll see a Domains link."
      fi
    else
      echo "Mailbox creation failed - see docs/SETUP.md section 6 to try again by hand." >&2
    fi
  fi
fi

cat <<EOF

==> Setup finished.

Mail ports:  SMTP 25, SMTPS 465, Submission 587, IMAP 143, IMAPS 993, POP3 110/995 - all public.
Panel:       127.0.0.1:\${PANEL_PORT:-3020} - put your own reverse proxy in front of it.
EOF

if [ -n "$MAILBOX_CREATED" ]; then
  echo "Log in at the panel with ${MAILBOX_CREATED} and the password you just set."
else
  echo "Next: bring the stack up and create your first mailbox - see docs/SETUP.md sections 3 and 6."
fi

cat <<'EOF'
To update later: ./update.sh
Full reference, DNS records, firewall rules, and the TLS tradeoff: docs/SETUP.md
EOF
