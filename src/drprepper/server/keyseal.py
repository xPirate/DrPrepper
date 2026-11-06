"""Unlock at start: keep the EHR key sealed under a passphrase instead of in a plain file.

Two modes, chosen by which file exists in the secrets folder:

* ``ehr.key``         plain key file (unattended start; the default, and what Docker uses)
* ``ehr.key.sealed``  key wrapped with scrypt + AES-256-GCM under a passphrase. The service starts LOCKED:
                      the drug and overdose reference works, but no patient data can be read until someone
                      enters the passphrase at /unlock. Nothing on the SD card alone opens the records.

The passphrase is never stored. After unlock the derived wrapping key stays in memory only, so a
restore can re-seal the restored key without asking again.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

AAD = b"drprepper-ehr-key-v1"
N, R, P = 2 ** 15, 8, 1
MIN_PASSPHRASE = 12


class SealError(Exception):
    pass


def sealed_path(s) -> Path:
    return s.key_file.with_name(s.key_file.name + ".sealed")


def derive(passphrase: str, salt: bytes, n: int = N, r: int = R, p: int = P) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())


def seal_with(key_hex: str, kek: bytes, salt: bytes, n: int = N, r: int = R, p: int = P) -> bytes:
    nonce = os.urandom(12)
    ct = AESGCM(kek).encrypt(nonce, key_hex.encode(), AAD)
    return json.dumps({"v": 1, "kdf": "scrypt", "n": n, "r": r, "p": p,
                       "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
                       "ct": base64.b64encode(ct).decode()}).encode()


def seal(key_hex: str, passphrase: str) -> tuple[bytes, bytes]:
    """Returns (sealed file bytes, wrapping key)."""
    salt = os.urandom(16)
    kek = derive(passphrase, salt)
    return seal_with(key_hex, kek, salt), kek


def unseal(blob: bytes, passphrase: str) -> tuple[str, bytes, bytes]:
    """Returns (key hex, wrapping key, salt). Raises SealError on a wrong passphrase or damaged file."""
    try:
        d = json.loads(blob)
        salt = base64.b64decode(d["salt"])
        kek = derive(passphrase, salt, d["n"], d["r"], d["p"])
        key = AESGCM(kek).decrypt(base64.b64decode(d["nonce"]), base64.b64decode(d["ct"]), AAD).decode()
    except (InvalidTag, ValueError, KeyError) as e:
        raise SealError("Wrong passphrase.") from e
    if len(key) != 64:
        raise SealError("The sealed key file is damaged.")
    return key, kek, salt


def shred(path: Path) -> None:
    """Overwrite then remove (best effort on flash media; the sealed file is the protection that matters)."""
    try:
        n = path.stat().st_size
        with open(path, "r+b") as f:
            f.write(secrets.token_bytes(max(n, 64)))
            f.flush()
            os.fsync(f.fileno())
        path.unlink()
    except FileNotFoundError:
        pass


class UnlockThrottle:
    """5 wrong passphrases per device per 5 minutes, 20 per hour overall."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fails: list[tuple[float, str]] = []
        self.history: list[str] = []          # written into the audit trail once the unit is unlocked

    def _prune(self):
        cut = time.monotonic() - 3600
        self.fails = [f for f in self.fails if f[0] > cut]

    def blocked(self, client: str) -> bool:
        with self.lock:
            self._prune()
            recent = [f for f in self.fails if f[1] == client and f[0] > time.monotonic() - 300]
            return len(recent) >= 5 or len(self.fails) >= 20

    def fail(self, client: str) -> None:
        with self.lock:
            self.fails.append((time.monotonic(), client))
            self.history.append(client)


def register(app, ctx) -> None:
    ehr, render, flash, form = ctx.ehr, ctx.render, ctx.flash, ctx.form
    s = ctx.s
    throttle = UnlockThrottle()

    @app.get("/unlock", response_class=HTMLResponse)
    async def unlock_form(req: Request):
        if not ehr.locked:
            return RedirectResponse("/", 303)
        return render(req, "unlock.html", None)

    @app.post("/unlock")
    async def unlock(req: Request):
        f = await form(req)
        client = ctx.client(req)
        if not ehr.locked:
            return RedirectResponse("/", 303)
        if throttle.blocked(client):
            flash(req, "Too many wrong passphrases. Wait a few minutes and try again.", "error")
            return RedirectResponse("/unlock", 303)
        try:
            ehr.unlock(f.get("passphrase", ""))
        except SealError:
            throttle.fail(client)
            time.sleep(1)
            flash(req, "That passphrase didn't unlock the records.", "error")
            return RedirectResponse("/unlock", 303)
        from .auditlog import audit
        with ehr.session() as c:
            if throttle.history:
                audit(c, None, "ehr_unlock_failed", detail=f"{len(throttle.history)} wrong passphrase(s) before unlock, "
                      f"from {', '.join(sorted(set(throttle.history)))}")
            audit(c, None, "ehr_unlocked", detail=f"from {client}", client=client)
        throttle.history.clear()
        flash(req, "Records unlocked. Sign in to continue.")
        return RedirectResponse("/login", 303)

    # ---------------- admin: switch modes, change the passphrase
    @app.get("/admin/encryption", response_class=HTMLResponse)
    async def enc_view(req: Request):
        user = ctx.current_user(req, admin=True)
        return render(req, "encryption.html", user, sealed=sealed_path(s).exists(), encrypted=ehr.encrypted,
                      key_file=s.key_file, min_len=MIN_PASSPHRASE)

    @app.post("/admin/encryption")
    async def enc_change(req: Request):
        user = ctx.current_user(req, admin=True)
        f = await form(req)
        action = f.get("action")
        from .auditlog import audit
        if not ehr.encrypted:
            raise HTTPException(400, "This unit runs without encryption (development mode).")
        new, again = f.get("new", ""), f.get("confirm", "")
        try:
            if action in ("seal", "change"):
                if len(new) < MIN_PASSPHRASE:
                    raise SealError(f"Use at least {MIN_PASSPHRASE} characters: a short phrase of unrelated words works well.")
                if new != again:
                    raise SealError("The passphrases don't match.")
            if action == "seal":
                if sealed_path(s).exists():
                    raise SealError("Already sealed.")
                ehr.seal_key(new)
                detail = "key sealed; the unit now starts locked"
            elif action == "change":
                if not sealed_path(s).exists():
                    raise SealError("The key isn't sealed yet.")
                unseal(sealed_path(s).read_bytes(), f.get("current", ""))
                ehr.seal_key(new)
                detail = "unlock passphrase changed"
            elif action == "unseal":
                if not sealed_path(s).exists():
                    raise SealError("The key isn't sealed.")
                unseal(sealed_path(s).read_bytes(), f.get("current", ""))
                ehr.unseal_key()
                detail = "key stored as a plain file again; the unit starts unattended"
            else:
                raise SealError("Unknown action.")
        except SealError as e:
            flash(req, str(e), "error")
            return RedirectResponse("/admin/encryption", 303)
        with ehr.session() as c:
            audit(c, user, "encryption_settings", detail=detail, client=ctx.client(req))
        flash(req, detail[0].upper() + detail[1:] + "." + (" Write the passphrase down and keep it apart from the unit: "
                                                          "without it the records can't be opened." if action != "unseal" else ""))
        return RedirectResponse("/admin/encryption", 303)
