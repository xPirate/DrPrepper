"""Room cleaning status and the wall-screen status board."""
from fastapi.testclient import TestClient

from conftest import Browser, audit_messages, pid_from


def loc(app, name):
    with app.state.ehr.session() as c:
        return c.execute("SELECT * FROM locations WHERE name = ?", (name,)).fetchone()


def setup_patients(app, new_browser):
    t = new_browser("triage")
    ids = []
    for last, first, allergy in [("Hayes", "Marcus", "Penicillin"), ("Ortiz", "Lily", "NKDA"), ("Reyes", "Tomas", "")]:
        t.get("/patients/new")
        r = t.post("/patients/new", {"last_name": last, "first_name": first, "allergies": allergy,
                                     "assign_to": str(app.state.uids["smith"]) if last != "Reyes" else ""})
        ids.append(pid_from(r))
    t.get("/")
    t.post(f"/patients/{ids[0]}/move", {"location_id": str(loc(app, "Room 1")["id"]), "back": "/"})
    t.post(f"/patients/{ids[1]}/move", {"location_id": str(loc(app, "Room 2")["id"]), "back": "/"})
    return t, ids


def test_room_needs_cleaning_when_emptied_then_cleaned(app, new_browser):
    t, (a, b, _c) = setup_patients(app, new_browser)
    assert loc(app, "Room 1")["state"] == "ready"
    t.post(f"/patients/{a}/move", {"location_id": str(loc(app, "Room 3")["id"]), "back": "/"})
    assert loc(app, "Room 1")["state"] == "dirty"
    s = new_browser("smith")
    s.get(f"/patients/{b}")
    s.post(f"/patients/{b}/discharge", {"kind": "discharged"})
    assert loc(app, "Room 2")["state"] == "dirty"
    census = t.get("/census").text
    assert "Needs cleaning" in census and "Start cleaning" in census
    # a viewer (e.g. cleaning staff) can mark cleaning and ready, but not out of service
    v = new_browser("viewer")
    v.get("/census")
    r1 = loc(app, "Room 1")["id"]
    v.post(f"/locations/{r1}/state", {"state": "cleaning"})
    assert loc(app, "Room 1")["state"] == "cleaning"
    v.post(f"/locations/{r1}/state", {"state": "ready"})
    assert loc(app, "Room 1")["state"] == "ready"
    assert v.post(f"/locations/{r1}/state", {"state": "out"}).status_code == 403
    # can't change an occupied room; out of service blocks moves
    r3 = loc(app, "Room 3")["id"]
    assert t.post(f"/locations/{r3}/state", {"state": "dirty"}).status_code == 409
    t.post(f"/locations/{r1}/state", {"state": "out"})
    assert t.post(f"/patients/{a}/move", {"location_id": str(r1), "back": "/"}).status_code == 409
    # moving into a dirty room is allowed but flagged
    r2 = loc(app, "Room 2")["id"]
    t.post(f"/patients/{a}/move", {"location_id": str(r2), "back": "/"})
    assert "It was marked &#39;needs cleaning&#39;" in t.get("/").text
    msgs = audit_messages(app)
    assert any("changed room status" in m and "Room 1: needs cleaning (patient" in m for m in msgs)
    assert any("Park, Dana changed room status" in m and "Room 1: cleaning -> ready" in m for m in msgs)
    # triage area and morgue never need cleaning
    assert loc(app, "Triage")["state"] == "ready"


def test_board_access(app, new_browser):
    setup_patients(app, new_browser)
    anon = new_browser()
    r = anon.get("/board")
    assert r.status_code == 403 and "hasn't been set up" in r.text and "Room 1" not in r.text
    assert anon.get("/board/data").status_code == 403
    # the Pi's own screen
    local = Browser(TestClient(app, follow_redirects=False, client=("127.0.0.1", 50000)))
    assert local.get("/board").status_code == 200
    # signed-in users
    assert new_browser("smith").get("/board").status_code == 200
    # display keys
    a = new_browser("admin")
    a.get("/admin/board")
    a.post("/admin/board/keys", {"name": "Hallway TV"})
    page = a.get("/admin/board").text
    key = page.split("/board?key=")[1].split("<")[0].strip()
    assert "/board?key=" not in a.get("/admin/board").text            # shown once
    tv = new_browser()
    r = tv.get(f"/board?key={key}")
    assert r.status_code == 303 and r.headers["location"] == "/board"
    assert tv.get("/board").status_code == 200 and tv.get("/board/data").status_code == 200
    assert tv.get("/patients").status_code == 303                       # the key opens the board, nothing else
    assert tv.get("/api/drugs?q=asp").status_code in (303, 401, 403)
    assert new_browser().get("/board?key=wrong").status_code == 403
    with app.state.ehr.session() as c:
        kid = c.execute("SELECT id, last_seen_at FROM display_keys").fetchone()
    assert kid["last_seen_at"]
    a.post(f"/admin/board/keys/{kid['id']}/revoke")
    assert tv.get("/board/data").status_code == 403
    # turning off local access
    a.post("/admin/board", {"mode": "dark"})
    assert local.get("/board").status_code == 403
    msgs = audit_messages(app)
    assert any("created a status board display key" in m and "Hallway TV" in m for m in msgs)
    assert any("status board screen connected" in m for m in msgs)
    assert any("revoked a status board display key" in m for m in msgs)


def test_board_is_sanitized_unless_names_turned_on(app, new_browser):
    setup_patients(app, new_browser)
    s = new_browser("smith")
    html = s.get("/board").text
    assert "Room 1" in html and "Occupied" in html and "Empty · ready" in html
    assert "Waiting for provider" in html and "Active patients" in html
    for secret in ("Hayes", "Ortiz", "Reyes", "Marcus", "Smith", "ALLERGY", "Penicillin"):
        assert secret not in html.split("</header>", 1)[1], secret
    assert "NAMES SHOWN" not in html
    a = new_browser("admin")
    a.get("/admin/board")
    a.post("/admin/board", {"names": "1", "mode": "dark", "allow_local": "1"})
    body = s.get("/board/data").text
    assert "M. Hayes" in body and "L. Ortiz" in body and "H. Smith EMT-B" in body and "ALLERGY" in body
    assert "Marcus" not in body and "Penicillin" not in body
    assert "NAMES SHOWN" in s.get("/board").text and "NAMES SHOWN" in body
    assert any("changed status board settings" in m and "patient names on" in m for m in audit_messages(app))
    # morgue hidden unless enabled
    assert "Morgue" not in body
    a.post("/admin/board", {"names": "1", "show_morgue": "1", "mode": "light", "allow_local": "1"})
    page = s.get("/board").text
    assert "Morgue" in page and 'data-mode="light"' in page


def test_short_name():
    from drprepper.server.board import short_name
    assert short_name("Marcus", "Hayes") == "M. Hayes"
    assert short_name("", "Hayes") == "Hayes"
    assert short_name(None, None) == "Unidentified"
