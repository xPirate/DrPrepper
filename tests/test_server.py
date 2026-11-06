import re

from conftest import audit_messages, login, pid_from


def test_login_required_bad_password_and_csrf(new_browser):
    b = new_browser()
    r = b.get("/")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    b.get("/login")
    assert b.post("/login", {"username": "admin", "password": "wrong"}).headers["location"] == "/login"
    login(b)
    assert b.c.post("/patients/new", data={"csrf": "nope", "last_name": "X"}).status_code == 400


def test_public_reference_without_login(new_browser):
    b = new_browser()
    assert b.get("/drugs?q=narcan").status_code == 200
    assert b.get("/tox/opioids").status_code == 200
    assert b.get("/api/drugs?q=nal").status_code == 303
    assert b.get("/trust/drprepper.crt").status_code in (200, 404)


def test_patient_number_and_audit_wording(app, new_browser):
    t = new_browser("triage")
    r = t.post("/patients/new", {"last_name": "Doe", "first_name": "Jane", "allergies": "NKDA"})
    pid = pid_from(r)
    with app.state.ehr.session() as c:
        pnum = c.execute("SELECT pnum FROM patients WHERE id = ?", (pid,)).fetchone()["pnum"]
    assert re.fullmatch(r"[1-9]\d{7}", pnum)
    t.get(f"/patients/{pid}")
    msgs = audit_messages(app)
    assert any(re.fullmatch(rf"\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d - Ortega, Maria RN accessed patient record {pnum}", m) for m in msgs), msgs[-3:]


def test_triage_assign_provider_handoff_and_justification(app, new_browser):
    uid = app.state.uids
    t = new_browser("triage")
    pid = pid_from(t.post("/patients/new", {"last_name": "Hayes", "first_name": "Marcus", "allergies": "Penicillin (hives)"}))
    # unassigned patients show on the triage board
    assert "Hayes" in t.get("/").text

    smith, williams = new_browser("smith"), new_browser("williams")
    # names and allergy badge visible on the list for everyone
    lst = smith.get("/patients").text
    assert "Hayes" in lst and "ALLERGIES" in lst
    # Smith can't open an unassigned chart without a reason
    r = smith.get(f"/patients/{pid}")
    assert r.status_code == 303 and "/access" in r.headers["location"]

    t.get("/")
    t.post(f"/patients/{pid}/assign", {"to_user": str(uid["smith"]), "back": "/"})
    assert smith.get(f"/patients/{pid}").status_code == 200
    assert "Hayes" in smith.get("/").text  # "My patients"

    # Williams (not assigned) must justify
    r = williams.get(f"/patients/{pid}")
    assert "/access" in r.headers["location"]
    williams.get(f"/patients/{pid}/access")
    r = williams.post(f"/patients/{pid}/access", {"reason": "Other (explain below)", "note": ""})
    assert "/access" in r.headers["location"]          # 'Other' needs details
    williams.get(f"/patients/{pid}/access")
    r = williams.post(f"/patients/{pid}/access", {"reason": "Covering for the assigned provider", "note": "Smith on break"})
    assert r.headers["location"] == f"/patients/{pid}"
    assert williams.get(f"/patients/{pid}").status_code == 200
    msgs = audit_messages(app)
    assert any("Williams, Abigail MD accessed patient record" in m and "without assignment. Reason: Covering for the assigned provider - Smith on break" in m
               for m in msgs)

    # Williams can't reassign someone else's patient
    williams.get(f"/patients/{pid}")
    assert williams.post(f"/patients/{pid}/assign", {"to_user": str(uid["williams"])}).status_code == 403
    # Smith hands off at end of shift
    smith.get(f"/patients/{pid}")
    r = smith.post(f"/patients/{pid}/assign", {"to_user": str(uid["williams"]), "note": "End of shift", "back": "/"})
    assert r.status_code == 303
    with app.state.ehr.session() as c:
        assert c.execute("SELECT assigned_to FROM patients WHERE id = ?", (pid,)).fetchone()["assigned_to"] == uid["williams"]
    assert any("Smith, Harold EMT-B handed off patient" in m and "to Williams, Abigail MD (note: End of shift)" in m
               for m in audit_messages(app))
    # Smith now needs a reason
    assert "/access" in smith.get(f"/patients/{pid}").headers["location"]


def test_last_seen_banner(app, new_browser):
    p = new_browser("smith")
    pid = pid_from(p.post("/patients/new", {"last_name": "Ortiz", "first_name": "Lily", "allergies": "NKDA"}))
    assert "Not yet seen" in p.get(f"/patients/{pid}").text
    eid = pid_from(p.post(f"/patients/{pid}/encounters", {"chief_complaint": "Check"}))
    p.get(f"/encounters/{eid}")
    p.post(f"/encounters/{eid}/vitals", {"hr": "90"})
    page = p.get(f"/patients/{pid}").text
    assert re.search(r"Last seen by: <b>Smith, Harold EMT-B</b> · \d{4}-\d\d-\d\d \d\d:\d\d", page)


def test_allergy_badge_states_and_popup_audit(app, new_browser):
    t = new_browser("triage")
    a = pid_from(t.post("/patients/new", {"last_name": "A", "allergies": "Sulfa (rash), Latex"}))
    n = pid_from(t.post("/patients/new", {"last_name": "N", "allergies": "nkda"}))
    u = pid_from(t.post("/patients/new", {"identifying": "Unknown female #2", "allergies": ""}))
    v = new_browser("viewer")
    page = v.get("/patients").text
    for pid, label in ((a, "ALLERGIES"), (n, "NKDA"), (u, "ALLERGIES UNKNOWN")):
        assert re.search(rf'data-allergy="{pid}"[^>]*>\s*{label}\s*<', page), label
    # The allergy list is not in the page; only the popup call returns it.
    assert "Sulfa" not in page
    r = v.api(f"/api/patients/{a}/allergies")
    assert r.json()["items"] == ["Sulfa (rash)", "Latex"] and r.json()["status"] == "allergies"
    assert v.api(f"/api/patients/{u}/allergies").json()["status"] == "unknown"
    assert v.c.post(f"/api/patients/{a}/allergies", headers={"X-CSRF-Token": "bad"}).status_code == 400
    assert any("Park, Dana viewed allergies for patient" in m for m in audit_messages(app))


def test_allergy_check_blocks_any_provider(app, new_browser):
    uid = app.state.uids
    t = new_browser("triage")
    pid = pid_from(t.post("/patients/new", {"last_name": "Doe", "allergies": "Naloxone (rash)", "assign_to": str(uid["smith"])}))
    w = new_browser("williams")
    w.get(f"/patients/{pid}/access")
    w.post(f"/patients/{pid}/access", {"reason": "Emergency: patient deteriorating"})
    eid = pid_from(w.post(f"/patients/{pid}/encounters", {"chief_complaint": "Unresponsive"}))
    hit = w.c.get("/api/drugs?q=naloxone").json()[0]
    w.get(f"/encounters/{eid}")
    w.post(f"/encounters/{eid}/meds", {"drug_id": str(hit["id"]), "drug_name": hit["name"], "dose": "4 mg", "route": "IN"})
    page = w.get(f"/encounters/{eid}").text
    assert "ALLERGY ALERT" in page
    w.post(f"/encounters/{eid}/meds", {"drug_id": str(hit["id"]), "drug_name": hit["name"], "dose": "4 mg", "route": "IN", "override": "1"})
    msgs = audit_messages(app)
    assert any("despite allergy alert" in m for m in msgs)


def test_viewer_read_only_and_admin_pages(app, new_browser):
    v = new_browser("viewer")
    assert v.post("/patients/new", {"last_name": "X"}).status_code == 403
    assert v.get("/admin/users").status_code == 403
    a = new_browser("admin")
    for page in ("/admin/users", "/admin/audit", "/admin/syslog", "/admin/trust", "/admin/backups"):
        assert a.get(page).status_code == 200, page


def test_user_create_edit_and_forced_password_change(app, new_browser):
    a = new_browser("admin")
    a.post("/admin/users", {"username": "hjones", "last_name": "Jones", "first_name": "Hal", "credential": "EMT-P", "role": "provider"})
    page = a.get("/admin/users").text
    assert "Jones, Hal EMT-P" in page
    pw = re.search(r'<code class="big">([^<]+)</code>', page).group(1)
    with app.state.ehr.session() as c:
        uid = c.execute("SELECT id FROM users WHERE username='hjones'").fetchone()["id"]
    a.get(f"/admin/users/{uid}")
    a.post(f"/admin/users/{uid}", {"last_name": "Jones", "first_name": "Hal", "credential": "RN", "role": "triage"})
    assert "Jones, Hal RN" in a.get("/admin/users").text
    assert any("updated user Jones, Hal RN" in m and "role 'provider' -> 'triage'" in m for m in audit_messages(app))
    b = new_browser()
    b.get("/login")
    b.post("/login", {"username": "hjones", "password": pw, "next": "/"})
    assert b.get("/").headers["location"] == "/account/password"


def test_idle_timeout(app, new_browser, settings):
    import time
    settings.idle_minutes = 1
    b = new_browser("smith")
    # forge an old 'seen' time by sleeping is too slow; patch time instead
    real = time.time
    try:
        time.time = lambda: real() + 120
        r = b.get("/")
    finally:
        time.time = real
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert any("signed out after inactivity" in m for m in audit_messages(app))


def test_ehr_file_encrypted(app, new_browser, settings):
    new_browser("triage").post("/patients/new", {"last_name": "Secretname"})
    raw = settings.ehr_db.read_bytes()
    assert b"SQLite format 3" not in raw[:16] and b"Secretname" not in raw


def test_security_headers(new_browser):
    r = new_browser().get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
