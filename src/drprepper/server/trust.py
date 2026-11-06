"""Trust center: inspect, generate, request and install the web console's TLS certificate.

The running server's SSL context is registered here at startup, so installing a
certificate takes effect for new connections immediately, without a restart.
"""
from __future__ import annotations

import ipaddress
import shutil
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config import write_private

LIVE_SSL: ssl.SSLContext | None = None   # set by `drprepper-manage serve`


def register_live_context(ctx: ssl.SSLContext | None) -> None:
    global LIVE_SSL
    LIVE_SSL = ctx


@dataclass
class CertInfo:
    subject: str
    issuer: str
    sans: list[str]
    not_before: datetime
    not_after: datetime
    fingerprint: str
    serial: str
    self_signed: bool
    key_type: str
    days_left: int
    key_matches: bool | None = None

    @property
    def status(self) -> str:
        if self.days_left < 0:
            return "expired"
        if self.days_left < 30:
            return "expiring"
        return "ok"


def _key_desc(pub) -> str:
    if isinstance(pub, rsa.RSAPublicKey):
        return f"RSA {pub.key_size}"
    if isinstance(pub, ec.EllipticCurvePublicKey):
        return f"ECDSA {pub.curve.name}"
    return type(pub).__name__


def _pub_bytes(pub) -> bytes:
    return pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def describe(cert_pem: bytes, key_pem: bytes | None = None) -> CertInfo:
    certs = x509.load_pem_x509_certificates(cert_pem)
    cert = certs[0]
    try:
        sans = [str(n.value) if not isinstance(n, x509.IPAddress) else f"IP {n.value}"
                for n in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value]
        sans = [s if s.startswith("IP ") else f"DNS {s}" for s in sans]
    except x509.ExtensionNotFound:
        sans = []
    na = cert.not_valid_after_utc
    info = CertInfo(
        subject=cert.subject.rfc4514_string(), issuer=cert.issuer.rfc4514_string(), sans=sans,
        not_before=cert.not_valid_before_utc, not_after=na,
        fingerprint=":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256())),
        serial=format(cert.serial_number, "X"), self_signed=cert.subject == cert.issuer,
        key_type=_key_desc(cert.public_key()), days_left=(na - datetime.now(timezone.utc)).days,
    )
    if key_pem is not None:
        key = serialization.load_pem_private_key(key_pem, password=None)
        info.key_matches = _pub_bytes(key.public_key()) == _pub_bytes(cert.public_key())
    return info


def current(cert_path: Path, key_path: Path) -> CertInfo | None:
    if not cert_path.exists():
        return None
    return describe(cert_path.read_bytes(), key_path.read_bytes() if key_path.exists() else None)


def _new_key(kind: str):
    return ec.generate_private_key(ec.SECP256R1()) if kind == "ecdsa" else rsa.generate_private_key(65537, 2048)


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def parse_names(text: str) -> list[x509.GeneralName]:
    names = []
    for raw in text.replace(",", "\n").splitlines():
        v = raw.strip()
        if not v:
            continue
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(v)))
        except ValueError:
            names.append(x509.DNSName(v))
    return names


def _subject(cn: str, org: str = "") -> x509.Name:
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    if org:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, org))
    return x509.Name(attrs)


def generate_self_signed(cn: str, names: str, days: int = 825, kind: str = "ecdsa", org: str = "") -> tuple[bytes, bytes]:
    key = _new_key(kind)
    sans = parse_names(names) or [x509.DNSName(cn)]
    subj = _subject(cn, org)
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(subj).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(t0).not_valid_after(t0 + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM), _key_pem(key)


def generate_csr(cn: str, names: str, kind: str = "ecdsa", org: str = "") -> tuple[bytes, bytes]:
    key = _new_key(kind)
    sans = parse_names(names) or [x509.DNSName(cn)]
    csr = (x509.CertificateSigningRequestBuilder().subject_name(_subject(cn, org))
           .add_extension(x509.SubjectAlternativeName(sans), critical=False).sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM), _key_pem(key)


def validate_pair(cert_pem: bytes, key_pem: bytes) -> CertInfo:
    try:
        info = describe(cert_pem, key_pem)
    except ValueError as e:
        raise ValueError(f"Could not read the certificate or key: {e}") from e
    if not info.key_matches:
        raise ValueError("The private key does not belong to this certificate.")
    if info.days_left < 0:
        raise ValueError(f"This certificate expired on {info.not_after:%Y-%m-%d}.")
    if info.not_before > datetime.now(timezone.utc) + timedelta(days=1):
        raise ValueError(f"This certificate is not valid until {info.not_before:%Y-%m-%d}. Check the device clock.")
    return info


def install(cert_path: Path, key_path: Path, cert_pem: bytes, key_pem: bytes) -> CertInfo:
    """Validate, keep the previous pair as *.prev, write the new pair, reload the live server."""
    info = validate_pair(cert_pem, key_pem)
    for p in (cert_path, key_path):
        if p.exists():
            shutil.copy2(p, p.with_name(p.name + ".prev"))
    write_private(key_path, key_pem)
    write_private(cert_path, cert_pem)
    cert_path.chmod(0o644)
    reload_live(cert_path, key_path)
    return info


def reload_live(cert_path: Path, key_path: Path) -> bool:
    if LIVE_SSL is None:
        return False
    LIVE_SSL.load_cert_chain(str(cert_path), str(key_path))
    return True


def ensure_default(cert_path: Path, key_path: Path, cn: str = "drprepper.local", names: str = "drprepper.local\n10.42.0.1\nlocalhost\n127.0.0.1") -> bool:
    if cert_path.exists() and key_path.exists():
        return False
    c, k = generate_self_signed(cn, names)
    write_private(key_path, k)
    write_private(cert_path, c)
    cert_path.chmod(0o644)
    return True


def der(cert_pem: bytes) -> bytes:
    return x509.load_pem_x509_certificates(cert_pem)[0].public_bytes(serialization.Encoding.DER)
