"""
`python manage.py createmailbox <email>` - a Django "management command"
(Django's mechanism for CLI scripts that share the app's settings/code; any
.py file under management/commands/ with a Command class becomes runnable
this way). This one exists purely to solve a chicken-and-egg problem: the
panel authenticates by trying a real IMAP login, so there's no way to log
into the panel *at all* until at least one mailbox exists - and there's no
separate admin UI to create that first one from. Run via:
    docker compose exec panel python manage.py createmailbox you@example.com
"""
import getpass
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from webmail import mailadmin

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Command(BaseCommand):
    help = (
        "Create a mailbox directly (bypassing the web panel) - mainly for "
        "bootstrapping the first account, since the panel itself needs an "
        "existing mailbox to log in with. Add the resulting email to "
        "ADMIN_EMAILS in .env to unlock the Domains admin section for it."
    )

    def add_arguments(self, parser):
        parser.add_argument("email", help="Full address to create, e.g. you@example.com")
        parser.add_argument(
            "--password",
            help="Mailbox password. Omit to be prompted (hidden input, recommended).",
        )
        parser.add_argument(
            "--quota",
            default=mailadmin.DEFAULT_QUOTA,
            help=f"Mailbox quota, e.g. 3G, 500M (default: {mailadmin.DEFAULT_QUOTA}).",
        )

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        if not _EMAIL_RE.match(email):
            raise CommandError(f"'{email}' doesn't look like a valid email address.")
        local_part, domain = email.split("@", 1)

        password = options["password"]
        if not password:
            password = getpass.getpass("Mailbox password: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                raise CommandError("Passwords didn't match.")
        if len(password) < 8:
            raise CommandError("Use at least 8 characters.")

        try:
            mailadmin.add_mailbox(domain, local_part, password, quota=options["quota"])
        except mailadmin.MailAdminError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Created {email}."))
        if email not in settings.ADMIN_EMAILS:
            self.stdout.write(
                f"To use the panel's Domains admin section as this account, add it to "
                f"ADMIN_EMAILS in .env (comma separated), then `docker compose up -d`."
            )
