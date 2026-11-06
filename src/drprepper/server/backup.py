"""Backup bundles, schedules and restore.

A bundle (`.drpbak`, a tar file) holds everything needed to move the EHR to new
hardware:

  manifest.json   format, time, source host, counts, SHA-256 of every file
  ehr.db          consistent copy of the encrypted EHR (still encrypted)
  secrets.enc     EHR key + TLS certificate/key, sealed with AES-256-GCM using a
                  key derived from the backup passphrase (scrypt)
  drugs.db        optional; the drug database can always be rebuilt

Without the passphrase a bundle is unreadable, so it is safe to keep on a USB
drive or network share. Without the passphrase it is also unrecoverable.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import socket
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .auditlog import audit
from .ehr import SCHEMA_VERSION, get_setting, now, set_setting

FORMAT = 1
AAD = b"drprepper-backup-v1"
KINDS = ("daily", "weekly", "monthly")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MIN_PASSPHRASE = 12
DEFAULTS = {
    "dest": "", "passphrase": "", "include_drugs": False,
    "daily": {"enabled": True, "time": "02:00", "keep": 7},
    "weekly": {"enabled": True, "time": "02:30", "weekday": 6, "keep": 4},
    "monthly": {"enabled": True, "time": "03:00", "day": 1, "keep": 12},
}


class BackupError(Exception):
    pass


def load_config(c, default_dest: str = "") -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    stored = get_setting(c, "backup", {}) or {}
    for k, v in stored.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    if not cfg["dest"]:
        cfg["dest"] = default_dest
    return cfg


# ------------------------------------------------------------------ crypto
def _kdf(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode())


def seal_secrets(payload: dict, passphrase: str) -> tuple[bytes, dict]:
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_kdf(passphrase, salt)).encrypt(nonce, json.dumps(payload).encode(), AAD)
    return ct, {"kdf": "scrypt", "n": 2 ** 15, "r": 8, "p": 1, "salt": base64.b64encode(salt).decode(),
                "nonce": base64.b64encode(nonce).decode(), "cipher": "AES-256-GCM"}


def open_secrets(ct: bytes, params: dict, passphrase: str) -> dict:
    try:
        key = _kdf(passphrase, base64.b64decode(params["salt"]))
        return json.loads(AESGCM(key).decrypt(base64.b64decode(params["nonce"]), ct, AAD))
    except Exception:
        raise BackupError("Wrong passphrase, or the backup's key file is damaged.")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ create
def bundle_name(kind: str, at: datetime | None = None) -> str:
    at = at or datetime.now()
    host = (socket.gethostname() or "drprepper").split(".")[0][:24]
    return f"drprepper-{host}-{at:%Y%m%d-%H%M%S}-{kind}.drpbak"


def create_bundle(ehr, out_dir: Path, kind: str, passphrase: str, include_drugs: bool = False) -> Path:
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise BackupError("Set a backup passphrase (12+ characters) before backing up.")
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        raise BackupError(f"Backup destination {out_dir} does not exist or is not mounted.")
    if not os.access(out_dir, os.W_OK):
        raise BackupError(f"Backup destination {out_dir} is not writable by the service.")
    s = ehr.s
    work = Path(tempfile.mkdtemp(prefix="drp-bk-", dir=s.state_dir))
    try:
        db_copy = work / "ehr.db"
        ehr.backup_to(db_copy)
        c = ehr.connect(db_copy)
        try:
            counts = {t: c.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                      for t in ("users", "patients", "encounters", "med_admin", "audit")}
            head = c.execute("SELECT id, hash FROM audit WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            c.close()
        payload = {"ehr_key": ehr.key, "encrypted": ehr.encrypted, "created": now(),
                   "tls_cert": s.tls_cert.read_text() if s.tls_cert.exists() else "",
                   "tls_key": s.tls_key.read_text() if s.tls_key.exists() else ""}
        ct, params = seal_secrets(payload, passphrase)
        (work / "secrets.enc").write_bytes(ct)
        files = ["ehr.db", "secrets.enc"]
        if include_drugs and s.drugs_db.exists():
            shutil.copy2(s.drugs_db, work / "drugs.db")
            files.append("drugs.db")
        manifest = {
            "format": FORMAT, "app": "drprepper", "schema_version": SCHEMA_VERSION, "kind": kind, "created": now(),
            "source_host": socket.gethostname(), "encrypted_ehr": ehr.encrypted, "secrets": params, "counts": counts,
            "audit_head": {"id": head["id"], "hash": head["hash"]} if head else None,
            "files": {f: {"sha256": _sha256(work / f), "size": (work / f).stat().st_size} for f in files},
        }
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
        final = out_dir / bundle_name(kind)
        partial = final.with_name(final.name + ".partial")
        with tarfile.open(partial, "w") as tar:
            for f in ["manifest.json", *files]:
                tar.add(work / f, arcname=f)
        with open(partial, "rb") as f:
            os.fsync(f.fileno())
        os.chmod(partial, 0o600)
        os.replace(partial, final)
        return final
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ read / verify
ALLOWED = {"manifest.json", "ehr.db", "secrets.enc", "drugs.db"}


def _extract(path: Path, into: Path) -> dict:
    try:
        with tarfile.open(path, "r") as tar:
            members = tar.getmembers()
            for m in members:
                if m.name not in ALLOWED or not m.isfile():
                    raise BackupError(f"Unexpected item in backup: {m.name!r}")
            for m in members:
                with tar.extractfile(m) as src, open(into / m.name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except tarfile.TarError as e:
        raise BackupError(f"Not a Dr.Prepper backup file ({e}).")
    try:
        manifest = json.loads((into / "manifest.json").read_text())
    except (OSError, ValueError):
        raise BackupError("Backup has no readable manifest.")
    if manifest.get("app") != "drprepper" or manifest.get("format") != FORMAT:
        raise BackupError("Not a Dr.Prepper backup, or made by an incompatible version.")
    for name, meta in manifest["files"].items():
        p = into / name
        if not p.exists() or _sha256(p) != meta["sha256"]:
            raise BackupError(f"{name} in this backup is damaged (checksum mismatch).")
    return manifest


def verify_bundle(ehr, path: Path, passphrase: str | None) -> dict:
    """Check structure and checksums; with the passphrase, also decrypt and open the database."""
    work = Path(tempfile.mkdtemp(prefix="drp-vf-", dir=ehr.s.state_dir))
    try:
        manifest = _extract(Path(path), work)
        result = {"file": Path(path).name, "manifest": manifest, "checksums": True, "decrypted": False, "integrity": None}
        if passphrase:
            sec = open_secrets((work / "secrets.enc").read_bytes(), manifest["secrets"], passphrase)
            result["decrypted"] = True
            c = ehr.connect(work / "ehr.db", key=sec["ehr_key"])
            try:
                result["integrity"] = c.execute("PRAGMA integrity_check").fetchone()["integrity_check"]
                result["patients"] = c.execute("SELECT COUNT(*) AS n FROM patients").fetchone()["n"]
            except Exception as e:
                raise BackupError(f"The database in this backup could not be opened: {e}")
            finally:
                c.close()
            if result["integrity"] != "ok":
                raise BackupError(f"Database integrity check failed: {result['integrity']}")
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ restore
def restore_bundle(ehr, path: Path, passphrase: str, restore_tls: bool = True, actor: str = "") -> dict:
    """Replace the EHR with a backup. The current data is kept in state_dir/pre-restore-<time>/."""
    s = ehr.s
    work = Path(tempfile.mkdtemp(prefix="drp-rs-", dir=s.state_dir))
    try:
        manifest = _extract(Path(path), work)
        sec = open_secrets((work / "secrets.enc").read_bytes(), manifest["secrets"], passphrase)
        if sec.get("encrypted", True) != ehr.encrypted:
            raise BackupError("This backup's encryption mode doesn't match this installation.")
        c = ehr.connect(work / "ehr.db", key=sec["ehr_key"])
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()["integrity_check"]
        finally:
            c.close()
        if ok != "ok":
            raise BackupError(f"Database integrity check failed: {ok}")

        ehr.maintenance.set()
        time.sleep(0.5)  # let in-flight requests finish
        try:
            keep = s.state_dir / f"pre-restore-{datetime.now():%Y%m%d-%H%M%S}"
            keep.mkdir(parents=True)
            for p in (s.ehr_db, s.ehr_db.with_name(s.ehr_db.name + "-wal"), s.ehr_db.with_name(s.ehr_db.name + "-shm")):
                if p.exists():
                    shutil.move(str(p), keep / p.name)
            from .keyseal import sealed_path
            src_key = s.key_file if s.key_file.exists() else sealed_path(s) if sealed_path(s).exists() else None
            if src_key:
                # The old key (plain or sealed) goes to the secrets folder, never next to the old database it unlocks.
                held = s.secrets_dir / f"{s.key_file.name}.{keep.name}"
                from .config import write_private as _wp
                _wp(held, src_key.read_bytes())
                (keep / "README.txt").write_text(f"Safety copy of the EHR from before a restore. Its key is held in the secrets folder as {held.name}.\n"
                                                 "Delete this copy from Admin > Backup & restore once the restore is confirmed.\n")
            if ehr.encrypted:
                ehr.store_key(sec["ehr_key"])      # stays sealed under the same passphrase if the unit uses one
            shutil.copy2(work / "ehr.db", s.ehr_db)
            os.chmod(s.ehr_db, 0o600)
            from ..ingest.__main__ import is_sample_db
            if (work / "drugs.db").exists() and is_sample_db(s.drugs_db):   # only replace a missing or sample database
                shutil.copy2(work / "drugs.db", s.drugs_db)
            tls_restored = False
            if restore_tls and sec.get("tls_cert") and sec.get("tls_key"):
                from . import trust
                try:
                    trust.install(s.tls_cert, s.tls_key, sec["tls_cert"].encode(), sec["tls_key"].encode())
                    tls_restored = True
                except ValueError:
                    tls_restored = False  # expired/mismatched cert in backup: keep the current one
            ehr.init_schema()  # upgrades an older backup's schema if needed
            with ehr.session() as c:   # sessions saved in the backup must not come back to life
                c.execute("UPDATE sessions SET revoked_at = ?, revoked_reason = 'restore' WHERE revoked_at IS NULL", (now(),))
        finally:
            ehr.maintenance.clear()
        with ehr.session() as c:
            audit(c, None, "restore", detail=f"{Path(path).name}, made {manifest['created'][:16]} on {manifest['source_host']}"
                  f"{'; by ' + actor if actor else ''}; previous data kept in {keep.name}")
        return {"manifest": manifest, "kept": str(keep), "tls_restored": tls_restored}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ listing / retention
def pre_restore_copies(s) -> list[dict]:
    out = []
    for p in sorted(s.state_dir.glob("pre-restore-*")):
        if p.is_dir():
            age = (datetime.now().timestamp() - p.stat().st_mtime) / 86400
            out.append({"name": p.name, "days": int(age), "size": sum(f.stat().st_size for f in p.glob("*") if f.is_file())})
    return out


def delete_pre_restore(s, name: str) -> None:
    p = s.state_dir / name
    if not name.startswith("pre-restore-") or "/" in name or not p.is_dir():
        raise BackupError("No such safety copy.")
    for f in p.glob("*"):
        if f.is_file():
            with open(f, "r+b") as fh:            # overwrite before unlinking (best effort on flash media)
                n = f.stat().st_size
                fh.write(b"\0" * min(n, 64 * 1024 * 1024))
            f.unlink()
    p.rmdir()
    key = s.secrets_dir / f"{s.key_file.name}.{name}"
    if key.exists():
        key.write_text("0" * 64)
        key.unlink()


def list_bundles(dest: str) -> list[dict]:
    d = Path(dest) if dest else None
    if not d or not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("drprepper-*.drpbak"), reverse=True):
        st = p.stat()
        kind = p.stem.rsplit("-", 1)[-1]
        out.append({"name": p.name, "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime), "kind": kind})
    return out


def prune(dest: Path, kind: str, keep: int) -> list[str]:
    files = sorted(Path(dest).glob(f"drprepper-*-{kind}.drpbak"))
    removed = []
    for p in files[: max(0, len(files) - max(1, keep))]:
        p.unlink(missing_ok=True)
        removed.append(p.name)
    return removed


def dest_warning(dest: str, state_dir: Path) -> str | None:
    """Warn when the destination is missing or on the same disk as the live data."""
    if not dest:
        return "No backup destination set."
    d = Path(dest)
    if not d.is_dir():
        return f"{dest} is not available. Is the USB drive or network share mounted?"
    try:
        if d.stat().st_dev == Path(state_dir).stat().st_dev:
            return f"{dest} is on the same storage as the live EHR, so it won't survive a disk failure. Use a USB drive or network share."
    except OSError:
        pass
    return None


# ------------------------------------------------------------------ schedule
def _at(day: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in (hhmm or "02:00").split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def last_due(kind: str, sched: dict, ref: datetime) -> datetime:
    """Most recent scheduled time at or before `ref`."""
    if kind == "daily":
        t = _at(ref, sched["time"])
        return t if t <= ref else t - timedelta(days=1)
    if kind == "weekly":
        t = _at(ref, sched["time"]) - timedelta(days=(ref.weekday() - int(sched["weekday"])) % 7)
        return t if t <= ref else t - timedelta(days=7)
    day = min(max(int(sched["day"]), 1), 28)
    t = _at(ref.replace(day=day), sched["time"])
    if t > ref:
        prev = (ref.replace(day=1) - timedelta(days=1)).replace(day=day)
        t = _at(prev, sched["time"])
    return t


def next_due(kind: str, sched: dict, ref: datetime) -> datetime:
    probe = ref + {"daily": timedelta(days=1), "weekly": timedelta(days=7), "monthly": timedelta(days=31)}[kind]
    return last_due(kind, sched, probe)


def run_backup(ehr, kind: str, actor: dict | None = None) -> dict:
    """Create one bundle at the configured destination, apply retention, record the run."""
    t0 = time.monotonic()
    with ehr.session() as c:
        cfg = load_config(c, ehr.s.backup_default_dest)
    try:
        path = create_bundle(ehr, Path(cfg["dest"]), kind, cfg["passphrase"], cfg.get("include_drugs", False))
        removed = prune(Path(cfg["dest"]), kind, int(cfg[kind]["keep"])) if kind in KINDS else []
        size = path.stat().st_size
        with ehr.session() as c:
            c.execute("INSERT INTO backup_runs(at, kind, dest, file, size, ok, seconds) VALUES (?,?,?,?,?,1,?)",
                      (now(), kind, cfg["dest"], path.name, size, time.monotonic() - t0))
            last = get_setting(c, "backup_last", {}) or {}
            last[kind] = now()
            set_setting(c, "backup_last", last)
            audit(c, actor, "backup_ok", detail=f"{kind}, {path.name}, {size / 1e6:.1f} MB"
                  + (f", removed {len(removed)} old" if removed else ""))
        return {"ok": True, "file": path.name, "size": size, "removed": removed}
    except Exception as e:
        with ehr.session() as c:
            c.execute("INSERT INTO backup_runs(at, kind, dest, ok, error, seconds) VALUES (?,?,?,0,?,?)",
                      (now(), kind, cfg["dest"], str(e)[:500], time.monotonic() - t0))
            if kind in KINDS:   # don't hammer a missing drive every minute; retry at the next check after 30 min
                last = get_setting(c, "backup_failed_at", {}) or {}
                last[kind] = now()
                set_setting(c, "backup_failed_at", last)
            audit(c, actor, "backup_fail", detail=f"{kind}: {e}")
        return {"ok": False, "error": str(e)}


def due_kinds(c, ref: datetime | None = None, default_dest: str = "") -> list[str]:
    ref = ref or datetime.now().astimezone().replace(tzinfo=None)
    cfg = load_config(c, default_dest)
    last = get_setting(c, "backup_last", {}) or {}
    failed = get_setting(c, "backup_failed_at", {}) or {}
    due = []
    for kind in KINDS:
        sched = cfg[kind]
        if not sched.get("enabled"):
            continue
        when = last_due(kind, sched, ref)
        done = datetime.fromisoformat(last[kind]).replace(tzinfo=None) if kind in last else None
        if done and done >= when:
            continue
        if kind in failed:
            f = datetime.fromisoformat(failed[kind]).replace(tzinfo=None)
            if ref - f < timedelta(minutes=30):
                continue
        due.append(kind)
    return due


class Scheduler(threading.Thread):
    def __init__(self, ehr, interval: float = 60.0):
        super().__init__(name="backup-scheduler", daemon=True)
        self.ehr, self.interval, self.stop_event = ehr, interval, threading.Event()

    def run(self):
        while not self.stop_event.wait(self.interval):
            if self.ehr.maintenance.is_set():
                continue
            try:
                c = self.ehr.connect()
                try:
                    kinds = due_kinds(c, default_dest=self.ehr.s.backup_default_dest)
                    cfg = load_config(c, self.ehr.s.backup_default_dest)
                finally:
                    c.close()
                if kinds and cfg["dest"] and cfg["passphrase"]:
                    # One bundle per check: if several are due (e.g. first boot), the largest period wins
                    # and the others are marked done by the same run.
                    kind = kinds[-1]
                    res = run_backup(self.ehr, kind)
                    if res["ok"]:
                        with self.ehr.session() as c:
                            last = get_setting(c, "backup_last", {}) or {}
                            for k in kinds:
                                last[k] = now()
                            set_setting(c, "backup_last", last)
            except Exception:  # never let the scheduler thread die
                pass
