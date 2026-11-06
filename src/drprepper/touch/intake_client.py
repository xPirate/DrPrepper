"""Send incoming-patient reports from the touchscreen to the EHR's triage board.

Uses the device token and the pinned server certificate from the kiosk folder.
The certificate is pinned by fingerprint, so a self-signed or CA-issued certificate
both work and nothing else on the network can impersonate the server.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization


class IntakeError(Exception):
    pass


def kiosk_dir() -> Path:
    return Path(os.environ.get("DRP_KIOSK_DIR", "/var/lib/drprepper/kiosk"))


def server_url() -> str:
    return os.environ.get("DRP_INTAKE_URL", "https://127.0.0.1:8443")


def _pin() -> str:
    pem = (kiosk_dir() / "server.crt").read_bytes()
    leaf = x509.load_pem_x509_certificates(pem)[0]
    return hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest()


def _token() -> str:
    return (kiosk_dir() / "token").read_text().strip()


def available() -> tuple[bool, str]:
    try:
        _token(); _pin()
        return True, ""
    except OSError:
        return False, f"This screen isn't set up to send reports (no token in {kiosk_dir()})."
    except Exception as e:
        return False, f"This screen's server certificate copy can't be read: {e}"


def _request(method: str, path: str, body: dict | None = None, timeout: float = 8.0) -> dict:
    try:
        token, pin = _token(), _pin()
    except OSError:
        raise IntakeError(f"This screen isn't set up to send reports (no token in {kiosk_dir()}).")
    u = urlsplit(server_url())
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # identity is checked by the pin below instead
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ctx)
    try:
        conn.connect()
        der = conn.sock.getpeercert(binary_form=True)
        if hashlib.sha256(der).hexdigest() != pin:
            raise IntakeError("The server's certificate doesn't match the one this screen trusts. Report not sent.")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        if r.status >= 400:
            detail = payload.get("detail")
            if isinstance(detail, list):   # validation errors
                detail = "; ".join(str(d.get("msg", d)) for d in detail)
            raise IntakeError(detail or f"The server refused the report ({r.status}).")
        return payload
    except (OSError, http.client.HTTPException) as e:
        raise IntakeError(f"Can't reach the triage server ({e.__class__.__name__}). Use the radio or a runner.")
    finally:
        conn.close()


def get_config() -> dict:
    return _request("GET", "/api/intake/config")


def send_report(report: dict) -> dict:
    return _request("POST", "/api/intake", report)
