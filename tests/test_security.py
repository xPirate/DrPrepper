"""Hardening from the 2026-10-22 security audit."""
import logging
import time

from conftest import PW, audit_messages


def test_unknown_user_costs_the_same_as_wrong_password(app, new_browser):
    from fastapi.testclient import TestClient
    from conftest import Browser
    from drprepper.server.auth import dummy_verify
    dummy_verify("warm up")
    n = iter(range(100, 200))

    def t(user):
        b = Browser(TestClient(app, follow_redirects=False, client=(f"10.42.1.{next(n)}", 5000)))
        b.get("/login")
        a = time.perf_counter()
        b.post("/login", {"username": user, "password": "not the password"})
        return time.perf_counter() - a
    known = t("smith")
    unknown = t("ghost1")
    print(known, unknown)
    assert abs(known - unknown) < max(0.25, known * 0.5)


def test_lockout_is_per_user_and_device(app, new_browser):
    from fastapi.testclient import TestClient
    from conftest import Browser
    attacker = Browser(TestClient(app, follow_redirects=False, client=("10.42.0.50", 5000)))
    attacker.get("/login")
    for _ in range(5):
        attacker.post("/login", {"username": "smith", "password": "wrong guess here"})
    r = attacker.post("/login", {"username": "smith", "password": PW, "next": "/"})
    assert r.headers["location"] == "/login"                      # even the right password is refused while locked
    assert any("login blocked after repeated failures (from 10.42.0.50)" in m for m in audit_messages(app))
    # the real provider on another tablet still gets in
    tablet = Browser(TestClient(app, follow_redirects=False, client=("10.42.0.51", 5000)))
    tablet.get("/login")
    r = tablet.post("/login", {"username": "smith", "password": PW, "next": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_password_blocklist_and_rehash(app, new_browser):
    from drprepper.server import auth
    assert auth.password_problem("password123") and auth.password_problem("aaaaaaaaaaaa")
    assert auth.password_problem("smith-is-my-name", "smith")
    assert auth.password_problem("correct horse battery staple") is None
    # an old 310k-round hash is upgraded at the next login
    old = "pbkdf2_sha256$1000$" + auth.hash_password(PW).split("$", 2)[2]
    import base64, hashlib, os
    salt = os.urandom(16)
    old = f"pbkdf2_sha256$1000${base64.b64encode(salt).decode()}${base64.b64encode(hashlib.pbkdf2_hmac('sha256', PW.encode(), salt, 1000)).decode()}"
    with app.state.ehr.session() as c:
        c.execute("UPDATE users SET pw_hash = ? WHERE username = 'smith'", (old,))
    new_browser("smith")
    with app.state.ehr.session() as c:
        assert c.execute("SELECT pw_hash FROM users WHERE username='smith'").fetchone()["pw_hash"].split("$")[1] == str(auth.ITERATIONS)


def test_headers_and_upload_limit(new_browser):
    b = new_browser()
    r = b.get("/login")
    assert "server" not in r.headers
    big = b.c.post("/patients/new", content=b"x" * 10, headers={"content-length": str(5 * 1024 ** 2),
                                                                 "content-type": "application/x-www-form-urlencoded"})
    assert big.status_code == 413


def test_access_log_drops_query_strings():
    from drprepper.server.manage import _NoQuery
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("10.42.0.5:1", "GET", "/patients?q=Hayes", "1.1", 200), None)
    _NoQuery().filter(rec)
    assert "Hayes" not in rec.getMessage() and "/patients?…" in rec.getMessage()


def test_syslog_copy_withholds_search_text(app, new_browser):
    from drprepper.server.syslog import format_5424
    s = new_browser("smith")
    s.get("/patients?q=Hayes")
    with app.state.ehr.session() as c:
        r = c.execute("SELECT * FROM audit WHERE action = 'patient_search' ORDER BY id DESC").fetchone()
    assert "Hayes" in r["message"]                         # the local, encrypted audit keeps it
    line = format_5424(r, {"facility": 20}).decode()
    assert "Hayes" not in line and "search text withheld" in line


def _clone(app, b):
    from fastapi.testclient import TestClient
    from conftest import Browser
    c = Browser(TestClient(app, follow_redirects=False))
    c.c.cookies.set("drp_session", b.c.cookies.get("drp_session"), domain="testserver.local")
    return c


def test_copied_cookie_dies_at_logout(app, new_browser):
    b = new_browser("smith")
    stolen = _clone(app, b)
    assert stolen.get("/").status_code == 200
    b.get("/")
    b.post("/logout")
    assert stolen.get("/").status_code == 303


def test_password_change_and_admin_actions_end_other_sessions(app, new_browser):
    tablet = new_browser("smith")
    laptop = new_browser("smith")
    laptop.get("/account/password")
    laptop.post("/account/password", {"current": PW, "new": "a new long passphrase", "confirm": "a new long passphrase"})
    assert laptop.get("/").status_code == 200            # the device that changed it stays in
    assert tablet.get("/").status_code == 303
    assert any("changed their password (signed out 1 other session(s))" in m for m in audit_messages(app))
    a = new_browser("admin")
    a.get("/admin/users")
    a.post(f"/admin/users/{app.state.uids['smith']}/signout")
    assert laptop.get("/").status_code == 303
    assert any("signed out all sessions of Smith, Harold EMT-B" in m for m in audit_messages(app))
    # role change
    w = new_browser("williams")
    a.get("/admin/users")
    a.post(f"/admin/users/{app.state.uids['williams']}", {"last_name": "Williams", "first_name": "Abigail",
                                                        "credential": "MD", "role": "viewer", "username": "williams"})
    assert w.get("/").status_code == 303


def test_clinical_text_keeps_earlier_versions(app, new_browser):
    from conftest import pid_from
    t = new_browser("triage")
    t.get("/patients/new")
    pid = pid_from(t.post("/patients/new", {"last_name": "Versioned", "allergies": "NKDA", "weight_kg": "70"}))
    t.get(f"/patients/{pid}")
    eid = int(t.post(f"/patients/{pid}/encounters", {"chief_complaint": "Chest pain"}).headers["location"].rsplit("/", 1)[1])
    t.post(f"/encounters/{eid}/update", {"chief_complaint": "Chest pain", "assessment": "Likely musculoskeletal"})
    t.post(f"/encounters/{eid}/update", {"chief_complaint": "Chest pain", "assessment": "Possible ACS - ECG ST changes"})
    page = t.get(f"/encounters/{eid}").text
    assert "Edit history (2 changes)" in page and "Likely musculoskeletal" in page
    t.get(f"/patients/{pid}/edit")
    t.post(f"/patients/{pid}/edit", {"last_name": "Versioned", "allergies": "NKDA", "weight_kg": "72"})
    t.post(f"/patients/{pid}/history", {"conditions": "HTN"})
    with app.state.ehr.session() as c:
        rows = c.execute("SELECT entity, field, old_value, new_value FROM revisions ORDER BY id").fetchall()
    fields = [(r["entity"], r["field"]) for r in rows]
    assert ("encounter", "assessment") in fields and ("patient", "weight_kg") in fields and ("patient", "conditions") in fields
    w = [r for r in rows if r["field"] == "weight_kg"][0]
    assert w["old_value"] == "70.0" and w["new_value"] == "72.0"
    assert "Edit history" in t.get(f"/patients/{pid}").text


def test_sealed_key_starts_locked_and_unlocks(app, new_browser, settings):
    from fastapi.testclient import TestClient
    from conftest import Browser
    from drprepper.server.app import create_app
    from drprepper.server.keyseal import sealed_path
    a = new_browser("admin")
    a.get("/admin/encryption")
    a.post("/admin/encryption", {"action": "seal", "new": "short", "confirm": "short"})
    assert not sealed_path(settings).exists()
    a.post("/admin/encryption", {"action": "seal", "new": "orange canoe at dawn", "confirm": "orange canoe at dawn"})
    assert sealed_path(settings).exists() and not settings.key_file.exists()
    assert b"orange" not in sealed_path(settings).read_bytes()
    # restart: locked
    app2 = create_app(settings, start_workers=False)
    b = Browser(TestClient(app2, follow_redirects=False))
    assert app2.state.ehr.locked
    assert b.get("/").headers["location"] == "/unlock"
    assert b.get("/patients").headers["location"] == "/unlock"
    assert b.get("/login").headers["location"] == "/unlock"
    assert b.get("/drugs?q=naloxone").status_code == 200          # the reference still works
    assert b.get("/tox").status_code == 200
    assert b.get("/healthz").json()["locked"] is True
    assert b.c.post("/api/intake", json={}).status_code == 503
    b.get("/unlock")
    b.post("/unlock", {"passphrase": "wrong one entirely"})
    assert app2.state.ehr.locked
    r = b.post("/unlock", {"passphrase": "orange canoe at dawn"})
    assert r.headers["location"] == "/login" and not app2.state.ehr.locked
    with app2.state.ehr.session() as c:
        msgs = [r["message"] for r in c.execute("SELECT message FROM audit ORDER BY id")]
    assert any("wrong unlock passphrase entered (1 wrong passphrase(s) before unlock" in m for m in msgs)
    assert any("unlocked the patient records at start-up" in m for m in msgs)
    # signing in works after unlock
    from conftest import login
    login(b, "smith")
    # change passphrase, then go back to a plain key file
    adm = Browser(TestClient(app2, follow_redirects=False))
    login(adm, "admin")
    adm.get("/admin/encryption")
    adm.post("/admin/encryption", {"action": "change", "current": "nope nope nope", "new": "blue kayak at noon", "confirm": "blue kayak at noon"})
    assert "Wrong passphrase" in adm.get("/admin/encryption").text
    adm.post("/admin/encryption", {"action": "change", "current": "orange canoe at dawn", "new": "blue kayak at noon", "confirm": "blue kayak at noon"})
    from drprepper.server.keyseal import unseal
    unseal(sealed_path(settings).read_bytes(), "blue kayak at noon")
    adm.post("/admin/encryption", {"action": "unseal", "current": "blue kayak at noon"})
    assert settings.key_file.exists() and not sealed_path(settings).exists()
    assert not create_app(settings, start_workers=False).state.ehr.locked


def test_restore_keeps_a_sealed_unit_sealed(app, new_browser, settings, tmp_path):
    from drprepper.server import backup
    from drprepper.server.app import create_app
    from drprepper.server.keyseal import sealed_path, unseal
    ehr = app.state.ehr
    bundle = backup.create_bundle(ehr, tmp_path, "daily", "backup passphrase 1")
    ehr.seal_key("unit unlock phrase")
    backup.restore_bundle(ehr, bundle, "backup passphrase 1")
    assert sealed_path(settings).exists() and not settings.key_file.exists()
    key, _, _ = unseal(sealed_path(settings).read_bytes(), "unit unlock phrase")
    assert key == ehr.key
    app2 = create_app(settings, start_workers=False)
    app2.state.ehr.unlock("unit unlock phrase")
    with app2.state.ehr.session() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] >= 5


def test_exit_pin_published_as_hash_only(app, new_browser, settings):
    from drprepper.server.auth import verify_password
    a = new_browser("admin")
    a.get("/admin/touchscreen")
    a.post("/admin/touchscreen", {"enabled": "1", "exit_pin": "123"})
    assert "6 to 8 digits" in a.get("/admin/touchscreen").text
    a.post("/admin/touchscreen", {"enabled": "1", "exit_pin": "48151623"})
    f = settings.kiosk_dir / "exit-pin"
    assert f.exists() and "48151623" not in f.read_text() and verify_password("48151623", f.read_text().strip())
    assert f.stat().st_mode & 0o777 == 0o640
    a.post("/admin/touchscreen", {"enabled": "1", "exit_clear": "1"})
    assert not f.exists()
    assert any("exit PIN removed" in m for m in audit_messages(app))


def test_audit_page_flags_bursts_of_justified_access(app, new_browser):
    from conftest import pid_from
    t = new_browser("triage")
    pids = []
    for i in range(5):
        t.get("/patients/new")
        pids.append(pid_from(t.post("/patients/new", {"last_name": f"Burst{i}", "allergies": "NKDA",
                                                      "assign_to": str(app.state.uids["williams"])})))
    s = new_browser("smith")
    for pid in pids:
        s.get(f"/patients/{pid}/access")
        s.post(f"/patients/{pid}/access", {"reason": "Quality or safety review"})
    s.get("/login"); s.post("/login", {"username": "nobody", "password": "x"})
    page = new_browser("admin").get("/admin/audit").text
    assert "opened 5 unassigned charts within an hour" in page and "Smith, Harold EMT-B" in page
    assert "5 charts opened with a reason" in page
