"""Building the space: no default layout, rooms with beds, numbered bulk add, remove vs turn off."""
from conftest import audit_messages, pid_from


def locs(app):
    with app.state.ehr.session() as c:
        return {r["label"]: r for r in c.execute("SELECT * FROM loc_v")}


def test_fresh_install_has_no_locations(settings):
    from drprepper.server.app import create_app
    app = create_app(settings, start_workers=False)
    with app.state.ehr.session() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM locations").fetchone()["n"] == 0


def test_rooms_with_beds(app, new_browser):
    a = new_browser("admin")
    a.get("/admin/locations")
    a.post("/admin/locations", {"name": "Ward A", "kind": "room", "capacity": "0"})
    ward = locs(app)["Ward A"]
    a.post("/admin/locations", {"name": "Bed", "count": "6", "start": "1", "parent_id": str(ward["id"])})
    l = locs(app)
    assert all(f"Ward A · Bed {i}" in l for i in range(1, 7)) and l["Ward A · Bed 1"]["kind"] == "room"
    # same bed names may exist in two rooms; duplicates within a room are refused
    a.post("/admin/locations", {"name": "Ward B", "kind": "room"})
    a.post("/admin/locations", {"name": "Bed", "count": "2", "parent_id": str(locs(app)["Ward B"]["id"])})
    assert "Ward B · Bed 1" in locs(app)
    a.post("/admin/locations", {"name": "Bed", "count": "2", "parent_id": str(ward["id"])})
    assert "Already exists in Ward A: Bed 1, Bed 2" in a.get("/admin/locations").text
    # top-level numbered beds with no room
    a.post("/admin/locations", {"name": "Cot", "kind": "other", "capacity": "1", "count": "3", "start": "1"})
    assert {"Cot 1", "Cot 2", "Cot 3"} <= set(locs(app))
    # patients go in a bed, not in the room itself
    t = new_browser("triage")
    t.get("/patients/new")
    pid = pid_from(t.post("/patients/new", {"last_name": "Hayes", "first_name": "Marcus", "allergies": "NKDA"}))
    t.get(f"/patients/{pid}")
    assert t.post(f"/patients/{pid}/move", {"location_id": str(ward["id"])}).status_code == 400
    bed3 = locs(app)["Ward A · Bed 3"]["id"]
    t.post(f"/patients/{pid}/move", {"location_id": str(bed3)})
    chart = t.get(f"/patients/{pid}").text
    assert "Ward A · Bed 3" in chart and '>Ward A</option>' not in chart
    census = t.get("/census").text
    assert "1/6 beds occupied" in census and "Bed 3" in census
    board = new_browser("smith").get("/board").text
    assert "Ward A" in board and "1/6 occupied" in board
    # leaving the bed marks that bed (not the whole room) for cleaning
    t.post(f"/patients/{pid}/move", {"location_id": str(locs(app)["Ward A · Bed 4"]["id"])})
    l = locs(app)
    assert l["Ward A · Bed 3"]["state"] == "dirty" and l["Ward A"]["state"] == "ready"
    assert any("added 6 beds in Ward A: Bed 1 - Bed 6" in m for m in audit_messages(app))
    # can't add beds directly into a room that has patients placed in it
    t.post(f"/patients/{pid}/move", {"location_id": str(l["Room 1"]["id"])})
    a.post("/admin/locations", {"name": "Bed", "count": "2", "parent_id": str(l["Room 1"]["id"])})
    assert "Move them before adding beds" in a.get("/admin/locations").text


def test_remove_unused_turn_off_used(app, new_browser):
    a = new_browser("admin")
    a.get("/admin/locations")
    l = locs(app)
    a.post(f"/admin/locations/{l['OR 2']['id']}/delete")
    assert "OR 2" not in locs(app)
    t = new_browser("triage")
    t.get("/patients/new")
    pid = pid_from(t.post("/patients/new", {"last_name": "Ortiz", "allergies": "NKDA"}))
    t.get(f"/patients/{pid}")
    t.post(f"/patients/{pid}/move", {"location_id": str(l["Room 2"]["id"])})
    a.post(f"/admin/locations/{l['Room 2']['id']}/delete")
    assert "has patients in it" in a.get("/admin/locations").text and locs(app)["Room 2"]["active"] == 1
    t.post(f"/patients/{pid}/move", {"location_id": str(l["Room 3"]["id"])})
    a.post(f"/admin/locations/{l['Room 2']['id']}/delete")
    assert "turned off instead of removed" in a.get("/admin/locations").text and locs(app)["Room 2"]["active"] == 0
    # the history still reads correctly
    assert "Room 2" in t.get(f"/patients/{pid}").text


def test_no_triage_area_means_no_location(app, new_browser):
    with app.state.ehr.session() as c:
        c.execute("DELETE FROM locations")
    t = new_browser("triage")
    t.get("/patients/new")
    pid = pid_from(t.post("/patients/new", {"last_name": "Brooks", "allergies": "NKDA"}))
    with app.state.ehr.session() as c:
        p = c.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    assert p["location_id"] is None and p["triage_start_at"]       # the triage clock still runs
    assert "No locations are set up yet" in t.get("/census").text
    a = new_browser("admin")
    assert "Start from a sample layout" in a.get("/admin/locations").text
    a.post("/admin/locations/sample")
    assert "Triage" in locs(app)


def test_discharge_kind_radios_render_as_rows(app, new_browser):
    css = new_browser().get("/static/style.css").text
    assert "input[type=radio] { width: auto;" in css


def test_v6_locations_upgrade_keeps_ids(app, new_browser):
    """A unit upgraded from the previous build keeps its rooms (and patients stay where they are)."""
    t = new_browser("triage")
    t.get("/patients/new")
    pid = pid_from(t.post("/patients/new", {"last_name": "Price", "allergies": "NKDA"}))
    r4 = locs(app)["Room 4"]["id"]
    t.get(f"/patients/{pid}")
    t.post(f"/patients/{pid}/move", {"location_id": str(r4)})
    ehr = app.state.ehr
    c = ehr.connect()
    c.execute("PRAGMA foreign_keys = OFF")
    c.executescript("""
      DROP VIEW loc_v; DROP INDEX loc_name;
      CREATE TABLE old_loc(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL COLLATE NOCASE, kind TEXT NOT NULL,
        capacity INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'ready', state_at TEXT, state_by INTEGER);
      INSERT INTO old_loc SELECT id, name, kind, capacity, active, sort, state, state_at, state_by FROM locations;
      DROP TABLE locations; ALTER TABLE old_loc RENAME TO locations;
      UPDATE patients SET triage_done_at = NULL WHERE assigned_to IS NOT NULL;
      PRAGMA user_version = 6;
    """)
    c.commit(); c.close()
    ehr.init_schema()
    with ehr.session() as c:
        assert "parent_id" in {r["name"] for r in c.execute("PRAGMA table_info(locations)")}
        assert c.execute("SELECT location_id FROM patients WHERE id = ?", (pid,)).fetchone()["location_id"] == r4
    assert "Room 4" in t.get(f"/patients/{pid}").text
