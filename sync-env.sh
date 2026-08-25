#!/usr/bin/env bash
#
# Regenerates the root .env - the file docker compose actually reads when
# run from here - from mailcow-dockerized/mailcow.conf + panel/.env.
#
# Run this any time you hand-edit either of those two files directly (e.g.
# adding ADMIN_EMAILS to panel/.env, or flipping MAIL_TLS_INSECURE, or
# changing something in mailcow.conf): docker compose won't see the change
# otherwise, since it only reads this merged file, not the originals.
# ./setup.sh already calls this as its last step.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in "${ROOT_DIR}/mailcow-dockerized/mailcow.conf" "${ROOT_DIR}/panel/.env"; do
  if [ ! -f "$f" ]; then
    echo "Missing $f - run ./setup.sh first." >&2
    exit 1
  fi
done

{
  grep -v '^COMPOSE_PROJECT_NAME=' "${ROOT_DIR}/mailcow-dockerized/mailcow.conf"
  cat "${ROOT_DIR}/panel/.env"
} > "${ROOT_DIR}/.env"
chmod 600 "${ROOT_DIR}/.env"

echo "Regenerated .env from mailcow-dockerized/mailcow.conf + panel/.env."
echo "Run 'docker compose up -d' to apply."
