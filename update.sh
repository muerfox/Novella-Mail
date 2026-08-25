#!/usr/bin/env bash
#
# update.sh - one command to update both containers to their latest version.
#
# What it does, in order:
#   1. Pulls the latest docker-mailserver image (Postfix/Dovecot/Rspamd).
#   2. Rebuilds the panel image from the current panel/ source (in case you
#      pulled newer code from git, e.g. `git pull` before running this).
#   3. Recreates only the containers that actually changed - docker compose
#      is smart about this, it won't touch containers whose image/config
#      didn't change.
#
# None of your mail data or domain/mailbox/DKIM config is affected: it all
# lives in docker volumes (mail-data, mail-state, mailserver-config, etc.),
# which are completely separate from the container images and survive an
# update untouched. Updating just swaps out the *code* the containers run.
#
# Run this from the repo root, same as setup.sh.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo "No .env found - run ./setup.sh first." >&2
  exit 1
fi

echo "==> Pulling the latest docker-mailserver image"
docker compose pull mailserver

echo "==> Rebuilding the panel image from panel/"
docker compose build panel

echo "==> Recreating any containers that changed"
docker compose up -d

cat <<'EOF'

==> Updated.

Check everything came back up cleanly:
  docker compose ps
  docker compose logs -f
EOF
