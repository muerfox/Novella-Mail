"""
`python manage.py ensurecerts` - create the self-signed TLS files
docker-mailserver needs when SSL_TYPE=self-signed. docker-mailserver doesn't
generate these itself: it refuses to start unless all three exist in its
config volume (target/scripts/helpers/ssl.sh):

    ssl/<MAIL_DOMAIN>-key.pem
    ssl/<MAIL_DOMAIN>-cert.pem
    ssl/demoCA/cacert.pem

docker-compose.yml runs this once (the `certs` service) before the mail
server starts. Existing files are left alone, so it's safe on every boot;
a new MAIL_DOMAIN just gets its own pair. Does nothing for other SSL_TYPEs.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

VALID_DAYS = 3650


def _write(path: Path, data: bytes, mode: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _pem_key(key):
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class Command(BaseCommand):
    help = "Create docker-mailserver's self-signed TLS certificate files if SSL_TYPE=self-signed and they're missing."

    def handle(self, *args, **options):
        ssl_type = os.environ.get("SSL_TYPE", "self-signed")
        if ssl_type != "self-signed":
            self.stdout.write(f"SSL_TYPE={ssl_type} - nothing to do.")
            return

        hostname = settings.MAIL_DOMAIN.strip().lower()
        if not hostname:
            raise CommandError("MAIL_DOMAIN is not set.")

        ssl_dir = Path(settings.MAIL_CONFIG_DIR) / "ssl"
        key_path = ssl_dir / f"{hostname}-key.pem"
        cert_path = ssl_dir / f"{hostname}-cert.pem"
        ca_path = ssl_dir / "demoCA" / "cacert.pem"
        ca_key_path = ssl_dir / "demoCA" / "private" / "cakey.pem"

        if key_path.exists() and cert_path.exists() and ca_path.exists():
            self.stdout.write(f"Self-signed certificate for {hostname} already present.")
            return

        now = datetime.now(timezone.utc)
        not_before = now - timedelta(minutes=5)
        not_after = now + timedelta(days=VALID_DAYS)

        # Reuse an existing CA (e.g. after MAIL_DOMAIN changed) so mail clients
        # that already trust it keep working.
        if ca_path.exists() and ca_key_path.exists():
            ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
            ca_cert = x509.load_pem_x509_certificate(ca_path.read_bytes())
        else:
            ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Novella-Mail CA ({hostname})")])
            ca_cert = (
                x509.CertificateBuilder()
                .subject_name(ca_name)
                .issuer_name(ca_name)
                .public_key(ca_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(not_before)
                .not_valid_after(not_after)
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=False, content_commitment=False, key_encipherment=False,
                        data_encipherment=False, key_agreement=False, key_cert_sign=True,
                        crl_sign=True, encipher_only=False, decipher_only=False,
                    ),
                    critical=True,
                )
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256())
            )
            _write(ca_key_path, _pem_key(ca_key), 0o600)
            _write(ca_path, ca_cert.public_bytes(serialization.Encoding.PEM), 0o644)

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False, key_encipherment=True,
                    data_encipherment=False, key_agreement=False, key_cert_sign=False,
                    crl_sign=False, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
            )
            .sign(ca_key, hashes.SHA256())
        )

        _write(key_path, _pem_key(key), 0o600)
        # Leaf first, then the CA: docker-mailserver uses this file as the chain.
        _write(
            cert_path,
            cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM),
            0o644,
        )
        self.stdout.write(self.style.SUCCESS(f"Created self-signed certificate for {hostname} in {ssl_dir}."))
