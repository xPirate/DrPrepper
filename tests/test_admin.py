from drprepper.server.ehr import SCHEMA_VERSION
import socket
import socketserver
import ssl
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from conftest import audit_messages, pid_from


# ------------------------------------------------------------------ audit chain
def test_audit_chain_verifies_and_detects_tampering(app, new_browser):
    from drprepper.server import auditlog
    t = new_browser("triage")
    for i in range(3):
        t.post("/patients/new", {"last_name": f"P{i}", "allergies": "NKDA"})
    ehr = app.state.ehr
    with ehr.session() as c:
        assert auditlog.verify(c)["ok"]
        assert c.execute("SELECT COUNT(*) AS n FROM audit WHERE hash IS NULL").fetchone()["n"] == 0
        mid = c.execute("SELECT id FROM audit ORDER BY id LIMIT 1 OFFSET 3").fetchone()["id"]
        c.execute("UPDATE audit SET message = message || ' (edited)' WHERE id = ?", (mid,))
    with ehr.session() as c:
        r = auditlog.verify(c)
    assert not r["ok"] and r["broken_id"] == mid


def test_audit_filters_and_csv_export(app, new_browser):
    t = new_browser("triage")
    pid = pid_from(t.post("/patients/new", {"last_name": "Filter"}))
    with app.state.ehr.session() as c:
        pnum = c.execute("SELECT pnum FROM patients WHERE id = ?", (pid,)).fetchone()["pnum"]
    a = new_browser("admin")
    page = a.get(f"/admin/audit?pnum={pnum}").text
    assert f"created patient record {pnum}" in page and "logged in" not in page
    csv = a.get(f"/admin/audit/export.csv?pnum={pnum}")
    assert csv.headers["content-type"].startswith("text/csv") and pnum in csv.text
    a.get("/admin/audit")
    a.post("/admin/audit/verify")
    assert "All " in a.get("/admin/audit").text
    assert any("exported the audit log" in m for m in audit_messages(app))


# ------------------------------------------------------------------ syslog
class Collector:
    def __init__(self, mode: str, ctx: ssl.SSLContext | None = None):
        self.msgs: list[bytes] = []
        me = self

        class TCP(socketserver.StreamRequestHandler):
            def handle(self):
                f = self.rfile
                while True:
                    head = b""
                    while (ch := f.read(1)) and ch != b" ":
                        head += ch
                    if not ch:
                        return
                    me.msgs.append(f.read(int(head)))

        class UDP(socketserver.BaseRequestHandler):
            def handle(self):
                me.msgs.append(self.request[0])

        if mode == "udp":
            self.srv = socketserver.ThreadingUDPServer(("127.0.0.1", 0), UDP)
        else:
            self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), TCP)
            if ctx:
                self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def wait(self, n, timeout=5):
        end = time.time() + timeout
        while len(self.msgs) < n and time.time() < end:
            time.sleep(0.05)
        return self.msgs

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.mark.parametrize("mode", ["udp", "tcp", "tls"])
def test_syslog_forwarding(app, new_browser, mode, tmp_path):
    from drprepper.server import syslog, trust
    ctx = None
    ca = ""
    if mode == "tls":
        cert, key = trust.generate_self_signed("localhost", "localhost\n127.0.0.1", 30)
        (tmp_path / "s.crt").write_bytes(cert); (tmp_path / "s.key").write_bytes(key)
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(tmp_path / "s.crt", tmp_path / "s.key")
        ca = cert.decode()
    col = Collector(mode, ctx)
    try:
        a = new_browser("admin")
        a.get("/admin/syslog")
        r = a.post("/admin/syslog", {"enabled": "1", "host": "127.0.0.1" if mode != "tls" else "localhost", "port": str(col.port),
                                     "transport": mode, "framing": "octet", "verify": "1", "ca_pem": ca, "facility": "20",
                                     "start_from": "keep"})
        assert r.status_code == 303
        t = new_browser("triage")
        t.post("/patients/new", {"last_name": "Syslogged"})
        sent = syslog.pump(app.state.ehr)
        assert sent > 3
        msgs = col.wait(sent)
        if mode != "udp":
            assert len(msgs) == sent, f"receiver got {len(msgs)} of {sent}"
        text = b"\n".join(msgs).decode()
        assert "created patient record" in text and "Ortega, Maria RN" in text
        assert text.count("<166>1 ") + text.count("<165>1 ") + text.count("<164>1 ") == len(msgs)  # local4 + severity
        assert 'drp@32473 id="' in text and 'hash="' in text
        with app.state.ehr.session() as c:
            st = syslog.status(c)
        assert st["pending"] == 0 and st["last_ok"]
        # test button
        a.get("/admin/syslog")
        a.post("/admin/syslog/test", {"host": "localhost" if mode == "tls" else "127.0.0.1", "port": str(col.port), "transport": mode,
                                      "framing": "octet", "verify": "1", "ca_pem": ca, "facility": "20"})
        assert any(b"syslog test message" in m for m in col.wait(len(msgs) + 1))
    finally:
        col.close()


def test_syslog_queues_while_server_down(app, new_browser):
    from drprepper.server import syslog
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()   # nothing listening
    a = new_browser("admin")
    a.get("/admin/syslog")
    a.post("/admin/syslog", {"enabled": "1", "host": "127.0.0.1", "port": str(port), "transport": "tcp", "facility": "20"})
    with pytest.raises(OSError):
        syslog.pump(app.state.ehr)
    with app.state.ehr.session() as c:
        assert syslog.status(c)["pending"] > 0
    col = Collector("tcp")
    try:
        a.get("/admin/syslog")
        a.post("/admin/syslog", {"enabled": "1", "host": "127.0.0.1", "port": str(col.port), "transport": "tcp", "facility": "20",
                                 "start_from": "keep"})
        n = syslog.pump(app.state.ehr)
        assert n > 0 and len(col.wait(n)) == n
    finally:
        col.close()


# ------------------------------------------------------------------ trust center
def test_trust_center_self_signed_csr_and_install(app, new_browser, settings):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from datetime import timedelta, timezone

    from drprepper.server import trust
    a = new_browser("admin")
    a.get("/admin/trust")
    a.post("/admin/trust/self-signed", {"cn": "unit7.local", "names": "unit7.local\n10.42.0.1", "days": "365", "kind": "ecdsa"})
    info = trust.current(settings.tls_cert, settings.tls_key)
    assert "unit7.local" in info.subject and "IP 10.42.0.1" in info.sans and info.key_matches
    assert "unit7.local" in a.get("/admin/trust").text
    assert a.get("/trust/drprepper.crt").content.startswith(b"-----BEGIN CERTIFICATE-----")

    # CSR flow, signed by a throwaway CA
    a.post("/admin/trust/csr", {"cn": "ehr.example.org", "names": "ehr.example.org", "kind": "ecdsa"})
    csr = x509.load_pem_x509_csr(a.get("/admin/trust/pending.csr").content)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "Test CA")])
    now = datetime.now(timezone.utc)
    signed = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca_name).public_key(csr.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
              .not_valid_after(now + timedelta(days=90))
              .add_extension(csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value, critical=False)
              .sign(ca_key, hashes.SHA256()))
    from cryptography.hazmat.primitives import serialization
    pem = signed.public_bytes(serialization.Encoding.PEM)
    a.get("/admin/trust")
    r = a.c.post("/admin/trust/install", data={"csrf": a.csrf}, files={"cert_file": ("c.crt", pem)})
    assert r.status_code == 303
    info = trust.current(settings.tls_cert, settings.tls_key)
    assert "ehr.example.org" in info.subject and "Test CA" in info.issuer and info.key_matches
    # wrong key is refused, current cert untouched
    other_cert, other_key = trust.generate_self_signed("x", "x", 10)
    a.get("/admin/trust")
    a.c.post("/admin/trust/install", data={"csrf": a.csrf}, files={"cert_file": ("c.crt", pem), "key_file": ("k.key", other_key)})
    assert "ehr.example.org" in trust.current(settings.tls_cert, settings.tls_key).subject
    # rollback to the previous (self-signed) one
    a.get("/admin/trust")
    a.post("/admin/trust/rollback")
    assert "unit7.local" in trust.current(settings.tls_cert, settings.tls_key).subject
    msgs = audit_messages(app)
    assert any("generated a new self-signed certificate" in m for m in msgs) and any("installed a certificate" in m for m in msgs)


def test_live_tls_reload(tmp_path):
    from drprepper.server import trust
    c1, k1 = trust.generate_self_signed("one.local", "one.local", 10)
    c2, k2 = trust.generate_self_signed("two.local", "two.local", 10)
    cp, kp = tmp_path / "t.crt", tmp_path / "t.key"
    cp.write_bytes(c1); kp.write_bytes(k1)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cp, kp)
    trust.register_live_context(ctx)
    try:
        trust.install(cp, kp, c2, k2)
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        port = srv.getsockname()[1]
        threading.Thread(target=lambda: ctx.wrap_socket(srv.accept()[0], server_side=True).close(), daemon=True).start()
        cli = ssl.create_default_context(); cli.check_hostname = False; cli.verify_mode = ssl.CERT_NONE
        with cli.wrap_socket(socket.create_connection(("127.0.0.1", port)), server_hostname="x") as s:
            der = s.getpeercert(binary_form=True)
        assert der == trust.der(c2)
    finally:
        trust.register_live_context(None)


# ------------------------------------------------------------------ backups
def test_backup_restore_roundtrip(app, new_browser, settings, tmp_path):
    from drprepper.server import backup
    dest = tmp_path / "usb"; dest.mkdir()
    a = new_browser("admin")
    a.get("/admin/backups")
    r = a.post("/admin/backups/settings", {"dest": str(dest), "passphrase": "short", "passphrase2": "short"})
    assert "at least 12" in a.get("/admin/backups").text
    a.post("/admin/backups/settings", {"dest": str(dest), "passphrase": "field kit passphrase", "passphrase2": "field kit passphrase",
                                       "daily_enabled": "1", "daily_time": "02:00", "daily_keep": "2"})
    t = new_browser("triage")
    pid = pid_from(t.post("/patients/new", {"last_name": "Before", "allergies": "Sulfa"}))
    a.get("/admin/backups")
    a.post("/admin/backups/run")
    files = backup.list_bundles(str(dest))
    assert len(files) == 1 and files[0]["kind"] == "manual"
    bundle = dest / files[0]["name"]
    raw = bundle.read_bytes()
    assert b"Before" not in raw and b"Sulfa" not in raw and settings.key_file.read_text().strip().encode() not in raw

    # verify
    assert backup.verify_bundle(app.state.ehr, bundle, "field kit passphrase")["integrity"] == "ok"
    with pytest.raises(backup.BackupError, match="Wrong passphrase"):
        backup.verify_bundle(app.state.ehr, bundle, "not the passphrase")

    # change data after the backup, then restore
    t.get("/patients")
    t.post("/patients/new", {"last_name": "After"})
    a.get("/admin/backups")
    r = a.post("/admin/backups/restore", {"name": bundle.name, "passphrase": "field kit passphrase", "confirm": "nope"})
    assert "#restore" in r.headers["location"]
    a.get("/admin/backups")
    r = a.post("/admin/backups/restore", {"name": bundle.name, "passphrase": "field kit passphrase", "confirm": "RESTORE",
                                          "restore_tls": "1"})
    assert r.headers["location"] == "/login"
    with app.state.ehr.session() as c:
        names = {r["last_name"] for r in c.execute("SELECT last_name FROM patients")}
        from drprepper.server import auditlog
        assert auditlog.verify(c)["ok"]
    assert "Before" in names and "After" not in names
    keep = list(settings.state_dir.glob("pre-restore-*"))
    assert keep and not list(keep[0].glob("*.key"))                    # the old key isn't left beside the old data
    held = settings.secret_file.parent / f"ehr.key.{keep[0].name}"
    assert held.exists() and held.stat().st_mode & 0o777 == 0o600
    assert any("restored the EHR from a backup" in m for m in audit_messages(app))
    with app.state.ehr.session() as c:
        assert not c.execute("SELECT 1 FROM sessions WHERE revoked_at IS NULL").fetchone()
    a = new_browser("admin")
    assert keep[0].name in a.get("/admin/backups").text
    a.post("/admin/backups/safety-delete", {"name": keep[0].name, "confirm": "DELETE"})
    assert not keep[0].exists() and not held.exists()
    assert any("deleted a pre-restore safety copy" in m for m in audit_messages(app))


def test_restore_on_new_hardware(app, new_browser, settings, tmp_path):
    """Bundle from unit A restores onto a fresh unit B with a different key."""
    from drprepper.server import backup
    from drprepper.server.config import Settings
    from drprepper.server.ehr import EHR
    dest = tmp_path / "usb"; dest.mkdir()
    new_browser("triage").post("/patients/new", {"last_name": "Moved"})
    path = backup.create_bundle(app.state.ehr, dest, "manual", "move this unit please")
    b = tmp_path / "unitB"; (b / "state").mkdir(parents=True)
    sb = Settings(drugs_db=settings.drugs_db, ehr_db=b / "state" / "ehr.db", key_file=b / "sec" / "ehr.key",
                  secret_file=b / "sec" / "session.secret", site_name="B", secure_cookies=False, session_hours=1,
                  allow_plaintext=False, state_dir=b / "state")
    eb = EHR(sb); eb.init_schema()
    assert eb.key != app.state.ehr.key
    backup.restore_bundle(eb, path, "move this unit please")
    assert eb.key == app.state.ehr.key == sb.key_file.read_text().strip()
    with eb.session() as c:
        assert c.execute("SELECT 1 FROM patients WHERE last_name = 'Moved'").fetchone()


def test_backup_schedule_and_retention(app, settings, tmp_path):
    from drprepper.server import backup
    from drprepper.server.ehr import set_setting
    dest = tmp_path / "usb"; dest.mkdir()
    ehr = app.state.ehr
    cfg = {"dest": str(dest), "passphrase": "scheduled passphrase", "daily": {"enabled": True, "time": "02:00", "keep": 2},
           "weekly": {"enabled": False, "time": "02:30", "weekday": 6, "keep": 4},
           "monthly": {"enabled": False, "time": "03:00", "day": 1, "keep": 12}}
    with ehr.session() as c:
        set_setting(c, "backup", cfg)
        assert backup.due_kinds(c, datetime(2026, 10, 22, 2, 5)) == ["daily"]
        assert backup.due_kinds(c, datetime(2026, 10, 22, 1, 59)) == ["daily"]  # yesterday's was never done
    for i in range(3):
        assert backup.run_backup(ehr, "daily")["ok"]
        time.sleep(1.05)   # distinct timestamps in file names
    assert len(list(dest.glob("*-daily.drpbak"))) == 2
    with ehr.session() as c:
        assert backup.due_kinds(c) == []
    # schedule maths
    s = {"time": "02:30", "weekday": 6}
    assert backup.last_due("weekly", s, datetime(2026, 10, 22, 12, 0)) == datetime(2026, 10, 18, 2, 30)   # Sunday
    assert backup.last_due("monthly", {"time": "03:00", "day": 1}, datetime(2026, 10, 1, 2, 0)) == datetime(2026, 9, 1, 3, 0)
    # missing destination fails, is audited, and doesn't retry for 30 minutes
    with ehr.session() as c:
        set_setting(c, "backup", {**cfg, "dest": str(tmp_path / "not-mounted")})
    assert not backup.run_backup(ehr, "daily")["ok"]
    with ehr.session() as c:
        c.execute("DELETE FROM settings WHERE key = 'backup_last'")
    with ehr.session() as c:
        assert backup.due_kinds(c) == []
    assert any("backup failed" in m for m in audit_messages(app))


def test_v1_database_migrates(settings, tmp_path):
    """A database from the first Docker build upgrades in place."""
    from drprepper.server.ehr import EHR
    ehr = EHR(settings)
    c = ehr.connect()
    c.executescript("""
      CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL, role TEXT NOT NULL,
        pw_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, must_change_pw INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
      CREATE TABLE patients(id INTEGER PRIMARY KEY, mrn TEXT UNIQUE NOT NULL, last_name TEXT, first_name TEXT, dob TEXT, age_est TEXT,
        sex TEXT, weight_kg REAL, allergies TEXT, conditions TEXT, home_meds TEXT, contact TEXT, identifying TEXT,
        created_at TEXT NOT NULL, created_by INTEGER, updated_at TEXT);
      CREATE TABLE audit(id INTEGER PRIMARY KEY, at TEXT NOT NULL, user_id INTEGER, action TEXT NOT NULL, entity TEXT, entity_id INTEGER, detail TEXT);
      INSERT INTO users VALUES (1, 'admin', 'Administrator', 'admin', 'x', 1, 0, '2026-10-21T10:00:00-05:00');
      INSERT INTO patients(id, mrn, last_name, created_at) VALUES (1, 'DP2610-0001', 'Old', '2026-10-21T10:00:00-05:00');
      INSERT INTO audit(at, user_id, action, detail) VALUES ('2026-10-21T10:00:00-05:00', 1, 'login_ok', 'x');
    """)
    c.commit(); c.close()
    ehr.init_schema()
    with ehr.session() as c:
        p = c.execute("SELECT * FROM patients").fetchone()
        assert p["mrn"] == "DP2610-0001" and len(p["pnum"]) == 8
        assert c.execute("SELECT last_name FROM users").fetchone()["last_name"] == "Administrator"
        assert c.execute("PRAGMA user_version").fetchone()["user_version"] == SCHEMA_VERSION
