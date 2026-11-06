"""Touchscreen intake: incoming-patient reports to the triage board."""
import threading
import time

import pytest

from conftest import audit_messages


def token(settings):
    return (settings.kiosk_dir / "token").read_text().strip()


def post(b, settings, body, tok=None):
    return b.c.post("/api/intake", json=body, headers={"Authorization": f"Bearer {tok or token(settings)}"})


REPORT = {"age_band": "adult", "age": "25", "sex": "female", "concern": "overdose", "avpu": "unresponsive",
          "breathing": "slow", "naloxone": "yes", "eta_min": 10, "location": "Medic 3", "notes": "Found down",
          "reporter": "Comms - Lee"}


def test_kiosk_folder_has_token_and_cert_but_no_patient_data(app, settings):
    files = {p.name for p in settings.kiosk_dir.iterdir()}
    assert "token" in files and len(token(settings)) > 30
    assert (settings.kiosk_dir / "token").stat().st_mode & 0o777 == 0o640
    assert not any(n.endswith(".db") or n.endswith(".key") for n in files)


def test_report_creates_incoming_patient_for_triage(app, new_browser, settings):
    k = new_browser()
    assert k.c.post("/api/intake", json=REPORT).status_code == 401
    assert post(k, settings, REPORT, tok="wrong").status_code == 401
    r = post(k, settings, REPORT)
    assert r.status_code == 201
    pnum = r.json()["pnum"]
    assert set(r.json()) == {"ok", "pnum", "eta_at"}          # write-only: nothing about other patients comes back

    with app.state.ehr.session() as c:
        p = c.execute("SELECT * FROM patients WHERE pnum = ?", (pnum,)).fetchone()
        e = c.execute("SELECT * FROM encounters WHERE patient_id = ?", (p["id"],)).fetchone()
    assert p["incoming"] == 1 and p["sex"] == "Female" and p["age_est"] == "~25" and p["allergies"] == "Unknown"
    assert "Unresponsive" in e["history"] and "Naloxone before arrival: Yes" in e["history"] and "Found down" in e["history"]
    assert e["location"] == "Medic 3"
    assert any(f"Touchscreen intake (Comms - Lee) sent an incoming patient report to triage: patient {pnum}" in m
               for m in audit_messages(app))

    t = new_browser("triage")
    board = t.get("/").text
    assert 'id="incoming"' in board and "unknown female, ~25, suspected overdose" in board and "ALLERGIES UNKNOWN" in board
    assert t.c.get("/api/triage/status").json()["incoming"] == 1
    # provider sees nothing on their board, and status shows zero for non-triage roles
    s = new_browser("smith")
    assert s.c.get("/api/triage/status").json()["incoming"] == 0

    # arrived -> moves to the waiting queue
    t.get("/")
    t.post(f"/patients/{p['id']}/arrived", {"back": "/"})
    with app.state.ehr.session() as c:
        assert c.execute("SELECT incoming, arrived_at FROM patients WHERE id = ?", (p["id"],)).fetchone()["incoming"] == 0
    board = t.get("/").text
    assert "Nobody is waiting." not in board
    assert any("marked incoming patient" in m for m in audit_messages(app))
    # providers can't mark arrivals
    s.get("/")
    assert s.post(f"/patients/{p['id']}/arrived").status_code == 403


def test_cancel_incoming_closes_encounter(app, new_browser, settings):
    pnum = post(new_browser(), settings, {**REPORT, "eta_min": None}).json()["pnum"]
    with app.state.ehr.session() as c:
        pid = c.execute("SELECT id FROM patients WHERE pnum = ?", (pnum,)).fetchone()["id"]
    t = new_browser("triage")
    t.post(f"/patients/{pid}/cancel-incoming", {"reason": "turned around, went to County"})
    with app.state.ehr.session() as c:
        e = c.execute("SELECT status, disposition FROM encounters WHERE patient_id = ?", (pid,)).fetchone()
    assert e["status"] == "closed" and "County" in e["disposition"]


def test_pin_disabled_and_rate_limit(app, new_browser, settings):
    a = new_browser("admin")
    a.get("/admin/touchscreen")
    a.post("/admin/touchscreen", {"enabled": "1", "require_pin": "1", "pin": "12ab"})
    assert "4 to 8 digits" in a.get("/admin/touchscreen").text
    a.post("/admin/touchscreen", {"enabled": "1", "require_pin": "1", "pin": "4321"})
    k = new_browser()
    assert k.c.get("/api/intake/config", headers={"Authorization": f"Bearer {token(settings)}"}).json()["pin_required"]
    assert post(k, settings, REPORT).status_code == 403
    assert post(k, settings, {**REPORT, "pin": "4321"}).status_code == 201
    a.get("/admin/touchscreen")
    a.post("/admin/touchscreen", {"require_pin": ""})          # enabled unticked -> off
    r = post(k, settings, {**REPORT, "pin": "4321"})
    assert r.status_code == 403 and "turned off" in r.json()["detail"]
    msgs = audit_messages(app)
    assert any("refused (wrong PIN)" in m for m in msgs) and any("refused (intake is turned off)" in m for m in msgs)


def test_token_rotation(app, new_browser, settings):
    old = token(settings)
    a = new_browser("admin")
    a.get("/admin/touchscreen")
    a.post("/admin/touchscreen/rotate")
    assert token(settings) != old
    k = new_browser()
    assert post(k, settings, REPORT, tok=old).status_code == 401
    assert post(k, settings, REPORT).status_code == 201


def test_validation_rejects_junk(new_browser, settings):
    k = new_browser()
    assert post(k, settings, {**REPORT, "eta_min": 99999}).status_code == 422
    assert post(k, settings, {**REPORT, "notes": "x" * 5000}).status_code == 422


def test_polling_does_not_count_as_activity(app, new_browser, settings):
    settings.idle_minutes = 1
    t = new_browser("triage")
    real = time.time
    try:
        time.time = lambda: real() + 50
        assert t.c.get("/api/triage/status").status_code == 200
        time.time = lambda: real() + 100              # 100 s since the last real activity
        r = t.c.get("/")
    finally:
        time.time = real
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_touch_client_over_pinned_tls(app, settings, tmp_path, monkeypatch):
    """The touchscreen's client against the real app over TLS, with certificate pinning."""
    import socket
    import uvicorn
    from drprepper.server import trust
    from drprepper.server.intake import publish_cert
    from drprepper.touch import intake_client

    trust.ensure_default(settings.tls_cert, settings.tls_key)
    publish_cert(settings)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                         ssl_certfile=str(settings.tls_cert), ssl_keyfile=str(settings.tls_key))
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    monkeypatch.setenv("DRP_KIOSK_DIR", str(settings.kiosk_dir))
    monkeypatch.setenv("DRP_INTAKE_URL", f"https://127.0.0.1:{port}")
    try:
        assert intake_client.available()[0]
        assert intake_client.get_config()["enabled"] is True
        res = intake_client.send_report(REPORT)
        assert len(res["pnum"]) == 8
        # a different certificate (e.g. an impostor on the network) is refused before anything is sent
        other, _ = trust.generate_self_signed("impostor", "impostor", 5)
        (settings.kiosk_dir / "server.crt").write_bytes(other)
        with pytest.raises(intake_client.IntakeError, match="doesn't match"):
            intake_client.send_report(REPORT)
        publish_cert(settings)
        # server down -> clear message, not a crash
        server.should_exit = True
        th.join(5)
        with pytest.raises(intake_client.IntakeError, match="Can't reach"):
            intake_client.send_report(REPORT)
    finally:
        server.should_exit = True
