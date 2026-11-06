"""Patient flow: triage timer, locations, discharge/AMA/transfer, death, readmit; history and home meds."""
from datetime import datetime, timedelta

from conftest import audit_messages, pid_from


def ago(minutes):
    return (datetime.now().astimezone() - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def new_patient(b, **kw):
    b.get("/patients/new")
    r = b.post("/patients/new", {"last_name": "Hayes", "first_name": "Marcus", "allergies": "NKDA", **kw})
    assert r.status_code == 303, r.text
    return pid_from(r)


def loc_id(app, name):
    with app.state.ehr.session() as c:
        return c.execute("SELECT id FROM locations WHERE name = ?", (name,)).fetchone()["id"]


def pat(app, pid):
    with app.state.ehr.session() as c:
        return c.execute("SELECT p.*, l.name AS loc FROM patients p LEFT JOIN locations l ON l.id = p.location_id WHERE p.id = ?",
                         (pid,)).fetchone()


def test_default_locations_seeded(app):
    with app.state.ehr.session() as c:
        kinds = {r["kind"] for r in c.execute("SELECT kind FROM locations")}
    assert kinds == {"triage", "room", "or", "other", "morgue"}


def test_triage_timer_runs_until_first_assignment(app, new_browser):
    t = new_browser("triage")
    pid = new_patient(t)
    p = pat(app, pid)
    assert p["loc"] == "Triage" and p["queued_at"] and p["triage_start_at"] and not p["triage_done_at"]
    # pretend they arrived 22 minutes ago
    with app.state.ehr.session() as c:
        c.execute("UPDATE patients SET triage_start_at = ?, queued_at = ? WHERE id = ?", (ago(22), ago(22), pid))
    board = t.get("/").text
    assert "In triage" in board and 'class="timer warn"' in board and "22 min" in board
    t.post(f"/patients/{pid}/assign", {"to_user": str(app.state.uids["smith"]), "back": "/"})
    p = pat(app, pid)
    assert p["triage_done_at"] and p["queued_at"] is None
    assert any("assigned patient" in m and "after 22 min in triage" in m for m in audit_messages(app))
    page = t.get(f"/patients/{pid}").text
    assert "Triage time" in page and "22 min" in page
    # handing off later doesn't change the triage time
    s = new_browser("smith")
    s.get(f"/patients/{pid}")
    s.post(f"/patients/{pid}/assign", {"to_user": str(app.state.uids["williams"]), "back": "/"})
    assert pat(app, pid)["triage_done_at"] == p["triage_done_at"]
    stats = t.get("/").text
    assert "Median triage time" in stats


def test_assigned_at_registration_is_zero_triage_and_incoming_starts_on_arrival(app, new_browser, settings):
    t = new_browser("triage")
    pid = new_patient(t, assign_to=str(app.state.uids["smith"]))
    p = pat(app, pid)
    assert p["triage_start_at"] == p["triage_done_at"] and p["loc"] == "Triage"
    # incoming via touchscreen: no triage clock until arrival
    tok = (settings.kiosk_dir / "token").read_text().strip()
    r = t.c.post("/api/intake", json={"age_band": "adult", "sex": "male", "concern": "trauma", "eta_min": 5},
                 headers={"Authorization": f"Bearer {tok}"})
    with app.state.ehr.session() as c:
        ip = c.execute("SELECT * FROM patients WHERE pnum = ?", (r.json()["pnum"],)).fetchone()
    assert ip["triage_start_at"] is None and ip["location_id"] is None
    t.get("/")
    t.post(f"/patients/{ip['id']}/arrived", {"back": "/"})
    ip = pat(app, ip["id"])
    assert ip["triage_start_at"] == ip["arrived_at"] and ip["loc"] == "Triage" and ip["queued_at"]


def test_move_permissions_capacity_and_history(app, new_browser):
    t = new_browser("triage")
    pid = new_patient(t, assign_to=str(app.state.uids["smith"]))
    r3 = loc_id(app, "Room 3")
    # a provider who isn't assigned can't move the patient
    w = new_browser("williams")
    w.get("/")
    assert w.post(f"/patients/{pid}/move", {"location_id": str(r3)}).status_code == 403
    s = new_browser("smith")
    s.get(f"/patients/{pid}")
    assert s.post(f"/patients/{pid}/move", {"location_id": str(r3), "note": "bed open"}).status_code == 303
    assert pat(app, pid)["loc"] == "Room 3"
    # morgue only through the death form
    assert s.post(f"/patients/{pid}/move", {"location_id": str(loc_id(app, "Morgue"))}).status_code == 400
    # second patient into the same 1-bed room: allowed but flagged
    pid2 = new_patient(t, last_name="Ortiz", assign_to=str(app.state.uids["smith"]))
    t.get("/")
    t.post(f"/patients/{pid2}/move", {"location_id": str(r3), "back": "/"})
    assert "capacity 1" in t.get("/").text
    page = s.get(f"/patients/{pid}").text
    assert "Movement history" in page and "Triage → <b>Room 3</b>" in page
    assert any("moved patient" in m and "Triage -> Room 3; note: bed open" in m for m in audit_messages(app))
    census = t.get("/census").text
    assert "Room 3" in census and "Hayes" in census and "Ortiz" in census and "over" in census


def test_discharge_and_ama_requirements(app, new_browser):
    t = new_browser("triage")
    pid = new_patient(t, assign_to=str(app.state.uids["smith"]))
    s = new_browser("smith")
    s.get(f"/patients/{pid}/discharge?kind=ama")
    r = s.post(f"/patients/{pid}/discharge", {"kind": "ama", "ama_capacity": "1", "ama_signature": "refused"})
    assert r.headers["location"].endswith("kind=ama")
    page = s.get(f"/patients/{pid}/discharge?kind=ama").text
    assert "confirm every item" in page and "witness is required" in page and "Describe the specific risks" in page
    assert pat(app, pid)["status"] == "active"
    # complete AMA
    s.get(f"/patients/{pid}")
    e = s.post(f"/patients/{pid}/encounters", {"chief_complaint": "Opioid OD"})
    eid = int(e.headers["location"].rsplit("/", 1)[1])
    r = s.post(f"/patients/{pid}/discharge", {
        "kind": "ama", "ama_capacity": "1", "ama_risks": "1", "ama_alternatives": "1", "ama_return": "1",
        "ama_risks_text": "Re-sedation when naloxone wears off, stopped breathing, death", "ama_signature": "refused",
        "witness": "Ortega, Maria RN", "condition": "Stable"})
    assert "/disposition/" in r.headers["location"]
    p = pat(app, pid)
    assert p["status"] == "ama" and p["location_id"] is None and p["disposition_at"]
    with app.state.ehr.session() as c:
        enc = c.execute("SELECT * FROM encounters WHERE id = ?", (eid,)).fetchone()
    assert enc["status"] == "closed" and enc["disposition"].startswith("Left AMA")
    printed = s.get(r.headers["location"] + "?print=1").text
    assert "Leaving against medical advice" in printed and "Patient refused to sign" in printed and "Ortega, Maria RN" in printed
    # gone from active boards, shown under "left care"
    board = t.get("/").text
    assert "Left care in the last 24 h" in board and "LEFT AMA" in board
    assert "Hayes" not in s.get("/").text.split("<main>")[1]
    # no more moves or assignments
    assert s.post(f"/patients/{pid}/move", {"location_id": str(loc_id(app, "Room 1"))}).status_code == 409
    assert any("leaving against medical advice" in m and "signature: refused" in m for m in audit_messages(app))
    # transfer needs a destination
    pid2 = new_patient(t, last_name="Reyes", assign_to=str(app.state.uids["smith"]))
    s.get(f"/patients/{pid2}/discharge?kind=transfer")
    s.post(f"/patients/{pid2}/discharge", {"kind": "transfer"})
    assert "Enter where the patient is going" in s.get(f"/patients/{pid2}/discharge?kind=transfer").text
    s.post(f"/patients/{pid2}/discharge", {"kind": "transfer", "destination": "Regional via Lifeflight 2"})
    assert pat(app, pid2)["status"] == "transfer"


def test_death_and_readmit(app, new_browser):
    t = new_browser("triage")
    pid = new_patient(t, assign_to=str(app.state.uids["smith"]))
    s = new_browser("smith")
    s.get(f"/patients/{pid}/death")
    s.post(f"/patients/{pid}/death", {"time_of_death": "", "pronounced_by": ""})
    page = s.get(f"/patients/{pid}/death").text
    assert "Enter the time of death" in page and "Tick the confirmation box" in page
    tod = datetime.now().strftime("%Y-%m-%dT%H:%M")
    s.post(f"/patients/{pid}/death", {"time_of_death": tod, "pronounced_by": "Williams, Abigail MD", "confirm": "1",
                                      "morgue_id": str(loc_id(app, "Morgue")), "nok_notified": "no"})
    p = pat(app, pid)
    assert p["status"] == "deceased" and p["loc"] == "Morgue"
    chart = t.get(f"/patients/{pid}").text
    assert "DECEASED" in chart and "Record of death" not in chart
    assert "Morgue" in t.get("/census").text
    # deceased can only be corrected (not "returned"), and only by triage/admin
    assert s.post(f"/patients/{pid}/readmit", {"why": "error", "note": "wrong patient"}).status_code == 403
    t.get(f"/patients/{pid}")
    t.post(f"/patients/{pid}/readmit", {"why": "error", "note": "Wrong chart selected"})
    p = pat(app, pid)
    assert p["status"] == "active" and p["loc"] == "Triage" and p["assigned_to"] == app.state.uids["smith"]
    assert any("corrected the disposition" in m and "Wrong chart selected" in m for m in audit_messages(app))
    with app.state.ehr.session() as c:
        assert c.execute("SELECT undone_at FROM dispositions WHERE patient_id = ?", (pid,)).fetchone()["undone_at"]
    # discharge, then the patient comes back: new triage, unassigned
    s.get(f"/patients/{pid}")
    s.post(f"/patients/{pid}/discharge", {"kind": "discharged"})
    t.get(f"/patients/{pid}")
    t.post(f"/patients/{pid}/readmit", {"why": "returned", "note": "Back with worse pain"})
    p = pat(app, pid)
    assert p["status"] == "active" and p["assigned_to"] is None and p["queued_at"] and not p["triage_done_at"]


def test_admin_locations(app, new_browser):
    a = new_browser("admin")
    a.get("/admin/locations")
    a.post("/admin/locations", {"name": "Decon tent", "kind": "other", "capacity": "0"})
    lid = loc_id(app, "Decon tent")
    assert "Decon tent" in a.get("/admin/locations").text
    # turning off the only triage area is allowed, with a warning; a room with a patient in it can't be turned off
    tri = loc_id(app, "Triage")
    a.post(f"/admin/locations/{tri}", {"name": "Triage", "kind": "triage", "capacity": "0", "sort": "0"})
    assert "no active triage area" in a.get("/admin/locations").text
    a.post(f"/admin/locations/{tri}", {"name": "Triage", "kind": "triage", "capacity": "0", "sort": "0", "active": "1"})
    t = new_browser("triage")
    pid = new_patient(t)
    t.get("/")
    t.post(f"/patients/{pid}/move", {"location_id": str(lid), "back": "/"})
    a.get("/admin/locations")
    a.post(f"/admin/locations/{lid}", {"name": "Decon", "kind": "other", "capacity": "0", "sort": "5"})
    assert "Move them before turning it off" in a.get("/admin/locations").text
    a.post(f"/admin/locations/{lid}", {"name": "Decon", "kind": "other", "capacity": "0", "sort": "5", "active": "1"})
    assert pat(app, pid)["loc"] == "Decon"
    a.post("/admin/locations-targets", {"warn": "10", "alert": "20"})
    with app.state.ehr.session() as c:
        c.execute("UPDATE patients SET queued_at = ? WHERE id = ?", (ago(12), pid))
    assert 'data-warn="10" data-alert="20"' in t.get("/").text
    assert any("changed locations" in m and "triage time targets" in m for m in audit_messages(app))


def test_history_and_home_meds(app, new_browser):
    t = new_browser("triage")
    pid = new_patient(t, assign_to=str(app.state.uids["smith"]))
    s = new_browser("smith")
    s.get(f"/patients/{pid}/history")
    s.post(f"/patients/{pid}/history", {"conditions": "Hypertension, T2DM", "surgical_hx": "Appendectomy 2010",
                                        "social_tobacco": "Current", "code_status": "DNR", "pregnancy": "Not applicable",
                                        "blood_type": "O+", "social_alcohol": "bogus"})
    p = pat(app, pid)
    assert p["code_status"] == "DNR" and p["social_alcohol"] is None and p["history_updated_by"] == app.state.uids["smith"]
    chart = s.get(f"/patients/{pid}").text
    assert "Appendectomy 2010" in chart and 'class="cbadge"' in chart
    assert 'class="cbadge"' in t.get("/").text          # DNR shows on the triage board like the allergy badge
    assert any("code status was 'not recorded', now 'DNR'" in m for m in audit_messages(app))
    # home meds
    s.post(f"/patients/{pid}/home-meds", {"name": "Acetaminophen", "dose": "500 mg", "frequency": "every 6 h",
                                          "last_dose": "today 08:00", "source": "Pill bottle or packaging"})
    s.post(f"/patients/{pid}/home-meds", {"name": "Lisinopril", "dose": "10 mg"})
    with app.state.ehr.session() as c:
        mids = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM patient_meds WHERE patient_id = ?", (pid,))}
    s.post(f"/patients/{pid}/home-meds/{mids['Lisinopril']}", {"status": "stopped"})
    chart = s.get(f"/patients/{pid}").text
    assert "Acetaminophen 500 mg every 6 h" in chart and 'class="stopped"' in chart
    # giving the same drug here warns about doubling up
    e = s.post(f"/patients/{pid}/encounters", {"chief_complaint": "Fever"})
    eid = int(e.headers["location"].rsplit("/", 1)[1])
    s.get(f"/encounters/{eid}")
    s.post(f"/encounters/{eid}/meds", {"drug_name": "Acetaminophen", "dose": "1 g", "route": "oral"})
    page = s.get(f"/encounters/{eid}").text
    assert "home medications include Acetaminophen 500 mg, last dose today 08:00" in page
    s.post(f"/encounters/{eid}/meds", {"drug_name": "Lisinopril", "dose": "10 mg", "route": "oral"})
    assert "home medications include" not in s.get(f"/encounters/{eid}").text      # stopped meds don't warn
    s.post(f"/encounters/{eid}/update", {"chief_complaint": "Fever", "exam": "Warm, flushed, lungs clear"})
    assert "Lungs clear" in s.get(f"/encounters/{eid}/print").text.replace("lungs", "Lungs")
    msgs = audit_messages(app)
    assert any("added a home medication" in m and "Acetaminophen 500 mg every 6 h" in m for m in msgs)
    assert any("updated a home medication" in m and "status taking -> stopped" in m for m in msgs)
    # viewer reads but can't edit
    v = new_browser("viewer")
    v.get(f"/patients/{pid}/access")
    v.post(f"/patients/{pid}/access", {"reason": "Quality or safety review"})
    assert v.post(f"/patients/{pid}/home-meds", {"name": "X"}).status_code == 403


def test_timezone_setting(app, new_browser, settings):
    import json
    import os
    import time
    from drprepper.server.ehr import now
    before = os.environ.get("TZ")
    try:
        a = new_browser("admin")
        a.get("/admin/time")
        a.post("/admin/time", {"timezone": "Not/AZone"})
        assert "isn&#39;t a known time zone" in a.get("/admin/time").text
        a.post("/admin/time", {"timezone": "America/Chicago"})
        assert now()[-6:] in ("-05:00", "-06:00")
        page = a.get("/admin/time").text
        assert "America/Chicago" in page and "UTC-0" in page
        assert json.loads((settings.kiosk_dir / "branding.json").read_text())["timezone"] == "America/Chicago"
        assert any("changed the time zone" in m and "-> America/Chicago" in m for m in audit_messages(app))
        # an entry written in UTC is shown in the unit's zone
        from drprepper.server.web import fmt_dt
        assert fmt_dt("2026-07-01T17:30:00+00:00") == "2026-07-01 12:30"
        # survives a restart
        from drprepper.server.app import create_app
        os.environ["TZ"] = "UTC"; time.tzset()
        create_app(settings, start_workers=False)
        assert os.environ["TZ"] == "America/Chicago"
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def test_review_fixes_triage_median_picker_unknown_allergy_ama_label(app, new_browser):
    t = new_browser("triage")
    t.get("/patients/new")
    t.post("/patients/new", {"last_name": "Fast", "allergies": "NKDA", "assign_to": str(app.state.uids["smith"])})
    pid = new_patient(t, last_name="Waited", allergies="Unknown")
    with app.state.ehr.session() as c:
        c.execute("UPDATE patients SET triage_start_at = ?, queued_at = ? WHERE id = ?", (ago(12), ago(12), pid))
    t.post(f"/patients/{pid}/assign", {"to_user": str(app.state.uids["smith"]), "back": "/"})
    from drprepper.server.flow import triage_stats
    with app.state.ehr.session() as c:
        st = triage_stats(c)
    assert st["n"] == 1 and st["median"] == 12          # the registration-time assignment is left out
    # picker: de-duplicated, oral first when asked
    rows = t.get("/api/drugs?q=acetaminophen&prefer=oral&limit=25").json()
    keys = [(r["name"].lower(), r["route"]) for r in rows]
    assert len(keys) == len(set(keys))
    if any("oral" in (r["route"] or "").lower() for r in rows):
        assert "oral" in (rows[0]["route"] or "").lower()
    # unknown allergies reminder on the medication form
    s = new_browser("smith")
    s.get(f"/patients/{pid}")
    eid = int(s.post(f"/patients/{pid}/encounters", {"chief_complaint": "x"}).headers["location"].rsplit("/", 1)[1])
    assert "Allergies not recorded" in s.get(f"/encounters/{eid}").text
    # AMA keeps its capitals
    s.get(f"/patients/{pid}")
    s.post(f"/patients/{pid}/discharge", {"kind": "ama", "ama_capacity": "1", "ama_risks": "1", "ama_alternatives": "1",
                                          "ama_return": "1", "ama_risks_text": "Explained risk of death", "ama_signature": "signed"})
    assert "left AMA" in s.get(f"/patients/{pid}").text or "Left AMA" in s.get(f"/patients/{pid}").text
    assert not any("left ama" in m for m in audit_messages(app))
