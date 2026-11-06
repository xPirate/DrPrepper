"""Patient flow: where each patient is, how long they waited in triage, and how they left.

* Triage time runs from arrival (or record creation) to the first assignment to a provider.
* Locations are admin-defined (triage, rooms, ORs, other areas, morgue). Every move is kept.
* Leaving care is a disposition: discharged, left AMA, transferred/evacuated, left without being
  seen, or deceased. Each has its own required fields; all of them can be printed.
"""
from __future__ import annotations

from datetime import datetime
from statistics import median

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .auditlog import audit
from .ehr import LOC_ORDER, get_setting, now, set_setting, triage_location
from .web import CLINICAL_WRITE

STATUS_LABELS = {
    "active": "In care", "discharged": "Discharged", "ama": "Left AMA", "transfer": "Transferred / evacuated",
    "lwbs": "Left without being seen", "deceased": "Deceased", "did_not_arrive": "Did not arrive",
}
KIND_LABELS = {"triage": "Triage", "room": "Rooms", "or": "Operating rooms", "other": "Other areas", "morgue": "Morgue"}
KIND_ONE = {"triage": "Triage", "room": "Room", "or": "OR", "other": "Area", "morgue": "Morgue"}
DISCHARGE_KINDS = {
    "discharged": "Discharged (released from care)",
    "ama": "Left against medical advice (AMA)",
    "transfer": "Transferred / evacuated to another facility",
    "lwbs": "Left without being seen",
}
CONDITIONS = ["Improved", "Stable", "Unchanged", "Guarded", "Critical"]
AMA_CHECKS = {
    "ama_capacity": "Patient is alert, oriented and has decision-making capacity",
    "ama_risks": "Risks of leaving were explained, including worsening illness, disability and death",
    "ama_alternatives": "Alternatives were offered (partial treatment, prescriptions, follow-up)",
    "ama_return": "Patient was told they can return at any time and will be treated",
}
AMA_SIGNATURE = {"signed": "Patient signed the AMA form", "refused": "Patient refused to sign"}
NOK = {"yes": "Yes", "no": "Not yet", "unable": "Unable to identify / reach"}
DEFAULT_TARGETS = {"warn": 15, "alert": 30}


# ------------------------------------------------------------------ time helpers
def lower_first(s: str) -> str:
    """'Left AMA' -> 'left AMA' (keeps acronyms intact, unlike str.lower)."""
    return s[:1].lower() + s[1:]


def _ts(v: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


def minutes(a: str | None, b: str | None) -> int | None:
    ta, tb = _ts(a), _ts(b)
    if not ta or not tb:
        return None
    return max(0, int((tb - ta).total_seconds() // 60))


def fmt_minutes(m: int | None) -> str:
    if m is None:
        return ""
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def targets(c) -> dict:
    t = dict(DEFAULT_TARGETS)
    t.update(get_setting(c, "triage_targets", {}) or {})
    return t


def triage_stats(c, hours: int = 24) -> dict:
    # Patients assigned while being registered (or pre-assigned before arrival) never waited: leave them out.
    rows = c.execute("SELECT triage_start_at, triage_done_at FROM patients WHERE triage_done_at IS NOT NULL "
                     "AND triage_done_at != triage_start_at").fetchall()
    cutoff = datetime.now().astimezone().timestamp() - hours * 3600
    mins = [m for r in rows if (_ts(r["triage_done_at"]) and _ts(r["triage_done_at"]).timestamp() >= cutoff)
            and (m := minutes(r["triage_start_at"], r["triage_done_at"])) is not None]
    return {"n": len(mins), "median": int(median(mins)) if mins else None, "max": max(mins) if mins else None, "hours": hours}


# ------------------------------------------------------------------ state changes (used by app.py and intake.py)
def _loc(r) -> dict:
    """A location row with 'name' as people should read it ('Room 1 · Bed 3'); 'own_name' is just 'Bed 3'."""
    return dict(r, own_name=r["name"], name=r["label"], active=r["live"])


def location(c, lid: int | None) -> dict | None:
    r = c.execute("SELECT * FROM loc_v WHERE id = ?", (lid,)).fetchone() if lid else None
    return _loc(r) if r else None


def all_locations(c, live_only: bool = True) -> list[dict]:
    q = "SELECT * FROM loc_v" + (" WHERE live = 1" if live_only else "") + f" ORDER BY {LOC_ORDER}"
    return [_loc(r) for r in c.execute(q).fetchall()]


def record_move(c, pid: int, kind: str, *, frm: str | None, to: dict | None, status: str, by: dict | None,
                note: str | None = None, at: str | None = None) -> None:
    c.execute("""INSERT INTO patient_moves(patient_id, at, kind, from_location, to_location, to_location_id, status, by_user, note)
                 VALUES (?,?,?,?,?,?,?,?,?)""",
              (pid, at or now(), kind, frm, to["name"] if to else None, to["id"] if to else None, status,
               by["id"] if by else None, note))


def enter_triage(c, pid: int, by: dict | None, at: str | None = None, kind: str = "arrive") -> None:
    """Patient is physically here: start the triage clock and put them in the triage area."""
    t = at or now()
    p = c.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    tri = location(c, triage_location(c))
    loc = location(c, p["location_id"]) or tri
    assigned = p["assigned_to"] is not None
    c.execute("""UPDATE patients SET triage_start_at = COALESCE(triage_start_at, ?),
                 triage_done_at = CASE WHEN ? AND triage_done_at IS NULL THEN ? ELSE triage_done_at END,
                 queued_at = CASE WHEN ? THEN NULL ELSE ? END,
                 location_id = ?, location_at = COALESCE(location_at, ?) WHERE id = ?""",
              (t, assigned, t, assigned, t, loc["id"] if loc else None, t, pid))
    if loc:
        record_move(c, pid, kind, frm=None, to=loc, status="active", by=by, at=t)


def on_assigned(c, pid: int, at: str) -> int | None:
    """Stop the triage clock on the first assignment. Returns the triage minutes when it just stopped."""
    p = c.execute("SELECT incoming, triage_start_at, triage_done_at FROM patients WHERE id = ?", (pid,)).fetchone()
    c.execute("UPDATE patients SET queued_at = NULL WHERE id = ?", (pid,))
    if p["incoming"] or not p["triage_start_at"] or p["triage_done_at"]:
        return None
    c.execute("UPDATE patients SET triage_done_at = ? WHERE id = ?", (at, pid))
    return minutes(p["triage_start_at"], at)


def on_unassigned(c, pid: int, at: str) -> None:
    c.execute("UPDATE patients SET queued_at = ? WHERE id = ? AND incoming = 0", (at, pid))


def occupancy(c) -> dict[int, int]:
    return {r["location_id"]: r["n"] for r in c.execute(
        "SELECT location_id, COUNT(*) AS n FROM patients WHERE status IN ('active','deceased') AND incoming = 0 "
        "AND location_id IS NOT NULL GROUP BY location_id")}


def location_options(c, include_morgue: bool = False) -> list[dict]:
    """Places a patient can be put: beds, and rooms/areas that have no beds inside them."""
    occ = occupancy(c)
    out = []
    for r in all_locations(c):
        if (r["kind"] == "morgue" and not include_morgue) or r["n_children"]:
            continue
        n = occ.get(r["id"], 0)
        r = dict(r, occupied=n, full=bool(r["capacity"] and n >= r["capacity"]))
        out.append(r)
    return out


ROOM_STATES = {"ready": "Ready", "dirty": "Needs cleaning", "cleaning": "Cleaning", "out": "Out of service"}


def cleanable(loc: dict | None) -> bool:
    """Beds, rooms, ORs and other spaces with a capacity get cleaned between patients; open areas and groups don't."""
    if not loc or loc.get("n_children") or loc["kind"] == "morgue":
        return False
    return bool(loc.get("parent_id")) or loc["kind"] in ("room", "or") or (loc["kind"] == "other" and loc["capacity"] > 0)


def vacated(c, loc: dict | None, by: dict | None, why: str) -> None:
    """Called after a patient leaves a location: an emptied room is marked as needing cleaning."""
    if not cleanable(loc):
        return
    if occupancy(c).get(loc["id"], 0):
        return
    t = now()
    c.execute("UPDATE locations SET state = 'dirty', state_at = ?, state_by = ? WHERE id = ? AND state != 'out'",
              (t, by["id"] if by else None, loc["id"]))
    audit(c, by, "room_state", detail=f"{loc['name']}: needs cleaning ({why})")


def can_move(ctx, user: dict, p: dict) -> bool:
    return user["role"] in CLINICAL_WRITE and ctx.can_handoff(user, p)


# ------------------------------------------------------------------ routes
def register(app, ctx) -> None:
    ehr, render, flash, form = ctx.ehr, ctx.render, ctx.flash, ctx.form
    tpl = ctx.tpl.env
    tpl.globals.update(ROOM_STATES=ROOM_STATES, cleanable=cleanable, STATUS_LABELS=STATUS_LABELS, KIND_LABELS=KIND_LABELS, KIND_ONE=KIND_ONE,
                       DISCHARGE_KINDS=DISCHARGE_KINDS, AMA_CHECKS=AMA_CHECKS, AMA_SIGNATURE=AMA_SIGNATURE, NOK=NOK)
    tpl.filters["mins"] = fmt_minutes
    tpl.globals["minutes_between"] = minutes

    def load(c, req, user, pid, write=True):
        p = ctx.get_patient(c, pid)
        ctx.require_chart(req, user, p, write=write)
        return p

    def need_active(p):
        if p["incoming"]:
            raise HTTPException(409, "This patient hasn't arrived yet. Mark them as arrived first.")
        if p["status"] != "active":
            raise HTTPException(409, f"This patient is no longer in care ({STATUS_LABELS.get(p['status'], p['status'])}).")

    def close_encounters(c, pid, text):
        c.execute("UPDATE encounters SET status = 'closed', closed_at = ?, disposition = COALESCE(NULLIF(disposition, ''), ?) "
                  "WHERE patient_id = ? AND status = 'open'", (now(), text, pid))

    # ---------------- census board
    @app.get("/census", response_class=HTMLResponse)
    async def census(req: Request):
        user = ctx.current_user(req)
        with ehr.session() as c:
            locs = all_locations(c)
            pts = ctx.patient_rows(c, "p.status IN ('active','deceased') AND p.incoming = 0 AND p.location_id IS NOT NULL",
                                   order="p.location_at")
            unplaced = ctx.patient_rows(c, "p.status = 'active' AND p.incoming = 0 AND p.location_id IS NULL")
            tg = targets(c)
        by_loc: dict[int, list] = {}
        for p in pts:
            by_loc.setdefault(p["location_id"], []).append(p)
        groups: dict[str, list] = {}
        tops: dict[int, dict] = {}
        for l in locs:
            item = dict(l, patients=by_loc.get(l["id"], []), beds=[])
            if l["parent_id"] and l["parent_id"] in tops:
                tops[l["parent_id"]]["beds"].append(item)
            elif not l["parent_id"]:
                tops[l["id"]] = item
                groups.setdefault(l["kind"], []).append(item)
        return render(req, "census.html", user, groups=groups, unplaced=unplaced, targets=tg, now_iso=now(),
                      no_locations=not locs)

    # ---------------- room status (cleaning)
    @app.post("/locations/{lid}/state")
    async def room_state(req: Request, lid: int):
        user = ctx.current_user(req)            # anyone signed in, including cleaning staff with viewer accounts
        f = await form(req)
        state = f.get("state")
        back = f.get("back") or "/census"
        back = back if back.startswith("/") and not back.startswith("//") else "/census"
        if state not in ROOM_STATES:
            raise HTTPException(400, "Unknown room status.")
        with ehr.session() as c:
            loc = location(c, lid)
            if not loc or not cleanable(loc):
                raise HTTPException(404, "Location not found")
            if (state == "out" or loc["state"] == "out") and user["role"] not in ("triage", "admin"):
                raise HTTPException(403, "Only triage or an admin can take a room in or out of service.")
            if occupancy(c).get(lid, 0):
                raise HTTPException(409, f"{loc['name']} has a patient in it.")
            if state != loc["state"]:
                c.execute("UPDATE locations SET state = ?, state_at = ?, state_by = ? WHERE id = ?", (state, now(), user["id"], lid))
                audit(c, user, "room_state", detail=f"{loc['name']}: {ROOM_STATES[loc['state']].lower()} -> {ROOM_STATES[state].lower()}",
                      client=ctx.client(req))
        flash(req, f"{loc['name']}: {ROOM_STATES[state].lower()}.")
        return RedirectResponse(back, 303)

    # ---------------- move
    @app.post("/patients/{pid}/move")
    async def move(req: Request, pid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        back = f.get("back") or f"/patients/{pid}"
        back = back if back.startswith("/") and not back.startswith("//") else f"/patients/{pid}"
        with ehr.session() as c:
            p = ctx.get_patient(c, pid)
            if not can_move(ctx, user, p):
                raise HTTPException(403, "Only triage, or the patient's assigned provider, can move this patient.")
            need_active(p)
            to = location(c, int(f["location_id"])) if (f.get("location_id") or "").isdigit() else None
            if not to or not to["active"]:
                raise HTTPException(400, "Pick a location.")
            if to["kind"] == "morgue":
                raise HTTPException(400, "Use 'Record death' to move a patient to the morgue.")
            if to["n_children"]:
                raise HTTPException(400, f"{to['name']} has beds. Pick one of its beds.")
            if to["id"] == p["location_id"]:
                flash(req, f"Already in {to['name']}.")
                return RedirectResponse(back, 303)
            if to["state"] == "out":
                raise HTTPException(409, f"{to['name']} is out of service.")
            frm = location(c, p["location_id"])
            n = occupancy(c).get(to["id"], 0)
            note = (f.get("note") or "").strip()[:300] or None
            t = now()
            c.execute("UPDATE patients SET location_id = ?, location_at = ?, updated_at = ? WHERE id = ?", (to["id"], t, t, pid))
            record_move(c, pid, "move", frm=frm["name"] if frm else None, to=to, status="active", by=user, note=note, at=t)
            audit(c, user, "patient_move", entity="patient", entity_id=pid, pnum=p["pnum"],
                  detail=f"{frm['name'] if frm else 'no location'} -> {to['name']}" + (f"; note: {note}" if note else ""),
                  client=ctx.client(req))
            vacated(c, frm, user, f"patient {p['pnum']} moved to {to['name']}")
        if to["state"] in ("dirty", "cleaning") and cleanable(to) and not n:
            flash(req, f"Moved to {to['name']}. It was marked '{ROOM_STATES[to['state']].lower()}'.", "error")
        elif to["capacity"] and n >= to["capacity"]:
            flash(req, f"Moved to {to['name']}. Note: {to['name']} now holds {n + 1} patients (capacity {to['capacity']}).", "error")
        else:
            flash(req, f"Patient {p['pnum']} moved to {to['name']}.")
        return RedirectResponse(back, 303)

    # ---------------- discharge / AMA / transfer / LWBS
    @app.get("/patients/{pid}/discharge", response_class=HTMLResponse)
    async def discharge_form(req: Request, pid: int, kind: str = "discharged"):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        with ehr.session() as c:
            p = load(c, req, user, pid)
            need_active(p)
        return render(req, "discharge.html", user, p=p, kind=kind if kind in DISCHARGE_KINDS else "discharged",
                      conditions=CONDITIONS, f=req.session.pop("dc_form", {}))

    @app.post("/patients/{pid}/discharge")
    async def discharge(req: Request, pid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        kind = f.get("kind")
        if kind not in DISCHARGE_KINDS:
            raise HTTPException(400, "Choose how the patient is leaving.")
        problems = []
        if kind == "ama":
            missing = [label for k, label in AMA_CHECKS.items() if f.get(k) != "1"]
            if missing:
                problems.append("For AMA, confirm every item: " + "; ".join(missing) + ".")
            if f.get("ama_signature") not in AMA_SIGNATURE:
                problems.append("Record whether the patient signed the AMA form.")
            if f.get("ama_signature") == "refused" and not f.get("witness"):
                problems.append("A witness is required when the patient refuses to sign.")
            if len(f.get("ama_risks_text") or "") < 10:
                problems.append("Describe the specific risks you explained.")
        if kind == "transfer" and not f.get("destination"):
            problems.append("Enter where the patient is going (facility, unit or evacuation asset).")
        if problems:
            for m in problems:
                flash(req, m, "error")
            req.session["dc_form"] = {k: v for k, v in f.items() if k != "csrf"}
            return RedirectResponse(f"/patients/{pid}/discharge?kind={kind}", 303)
        with ehr.session() as c:
            p = load(c, req, user, pid)
            need_active(p)
            t = now()
            cur = c.execute("""INSERT INTO dispositions(patient_id, at, kind, by_user, condition, destination, instructions, followup,
                                   ama_capacity, ama_risks, ama_alternatives, ama_return, ama_risks_text, ama_signature, witness)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (pid, t, kind, user["id"], f.get("condition") or None, f.get("destination") or None,
                             f.get("instructions") or None, f.get("followup") or None,
                             *(1 if kind == "ama" and f.get(k) == "1" else None for k in AMA_CHECKS),
                             f.get("ama_risks_text") if kind == "ama" else None,
                             f.get("ama_signature") if kind == "ama" else None, f.get("witness") or None))
            frm = location(c, p["location_id"])
            c.execute("UPDATE patients SET status = ?, disposition_at = ?, location_id = NULL, location_at = ?, queued_at = NULL, "
                      "updated_at = ? WHERE id = ?", (kind, t, t, t, pid))
            record_move(c, pid, "discharge", frm=frm["name"] if frm else None, to=None, status=kind, by=user,
                        note=f.get("destination") or None, at=t)
            vacated(c, frm, user, f"patient {p['pnum']} {lower_first(STATUS_LABELS[kind])}")
            summary = STATUS_LABELS[kind] + (f" to {f['destination']}" if f.get("destination") else "") + f" ({t[:16].replace('T', ' ')})"
            close_encounters(c, pid, summary)
            ctx.mark_seen(c, pid, user)
            detail = summary + (f"; signature: {f.get('ama_signature')}" if kind == "ama" else "")
            audit(c, user, f"patient_{kind}", entity="patient", entity_id=pid, pnum=p["pnum"], detail=detail, client=ctx.client(req))
        flash(req, f"Patient {p['pnum']}: {lower_first(STATUS_LABELS[kind])}.")
        return RedirectResponse(f"/patients/{pid}/disposition/{cur.lastrowid}", 303)

    # ---------------- death
    @app.get("/patients/{pid}/death", response_class=HTMLResponse)
    async def death_form(req: Request, pid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        with ehr.session() as c:
            p = load(c, req, user, pid)
            need_active(p)
            morgues = [l for l in all_locations(c) if l["kind"] == "morgue" and not l["n_children"]]
        return render(req, "death.html", user, p=p, morgues=morgues, f=req.session.pop("dc_form", {}))

    @app.post("/patients/{pid}/death")
    async def death(req: Request, pid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        tod = f.get("time_of_death")
        try:
            tod_iso = datetime.fromisoformat(tod).astimezone().isoformat(timespec="minutes") if tod else None
        except ValueError:
            tod_iso = None
        problems = []
        if not tod_iso:
            problems.append("Enter the time of death.")
        elif _ts(tod_iso) > datetime.now().astimezone():
            problems.append("The time of death can't be in the future.")
        if not f.get("pronounced_by"):
            problems.append("Enter who pronounced death.")
        if f.get("confirm") != "1":
            problems.append("Tick the confirmation box.")
        if problems:
            for m in problems:
                flash(req, m, "error")
            req.session["dc_form"] = {k: v for k, v in f.items() if k != "csrf"}
            return RedirectResponse(f"/patients/{pid}/death", 303)
        with ehr.session() as c:
            p = load(c, req, user, pid)
            need_active(p)
            morgue = location(c, int(f["morgue_id"])) if (f.get("morgue_id") or "").isdigit() else None
            if morgue and morgue["kind"] != "morgue":
                morgue = None
            t = now()
            cur = c.execute("""INSERT INTO dispositions(patient_id, at, kind, by_user, time_of_death, pronounced_by, death_notes,
                                   nok_notified, tag_number, effects) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (pid, t, "deceased", user["id"], tod_iso, f["pronounced_by"][:120], f.get("death_notes") or None,
                             f.get("nok_notified") if f.get("nok_notified") in NOK else None, f.get("tag_number") or None,
                             f.get("effects") or None))
            frm = location(c, p["location_id"])
            c.execute("UPDATE patients SET status = 'deceased', disposition_at = ?, location_id = ?, location_at = ?, queued_at = NULL, "
                      "updated_at = ? WHERE id = ?", (tod_iso, morgue["id"] if morgue else None, t, t, pid))
            record_move(c, pid, "death", frm=frm["name"] if frm else None, to=morgue, status="deceased", by=user, at=t)
            vacated(c, frm, user, f"patient {p['pnum']} moved to {morgue['name'] if morgue else 'no location'}")
            close_encounters(c, pid, f"Deceased. Time of death {tod_iso[:16].replace('T', ' ')}, pronounced by {f['pronounced_by']}")
            ctx.mark_seen(c, pid, user)
            audit(c, user, "patient_deceased", entity="patient", entity_id=pid, pnum=p["pnum"],
                  detail=f"time of death {tod_iso[:16].replace('T', ' ')}, pronounced by {f['pronounced_by'][:120]}"
                         + (f", to {morgue['name']}" if morgue else ""), client=ctx.client(req))
        flash(req, f"Death recorded for patient {p['pnum']}.")
        return RedirectResponse(f"/patients/{pid}/disposition/{cur.lastrowid}", 303)

    # ---------------- disposition record / print
    def get_disp(c, pid, did):
        d = c.execute("""SELECT d.*, u.full_name AS by_name, x.full_name AS undone_name FROM dispositions d
                         LEFT JOIN users u ON u.id = d.by_user LEFT JOIN users x ON x.id = d.undone_by
                         WHERE d.id = ? AND d.patient_id = ?""", (did, pid)).fetchone()
        if not d:
            raise HTTPException(404, "Record not found")
        return d

    @app.get("/patients/{pid}/disposition/{did}", response_class=HTMLResponse)
    async def disposition_view(req: Request, pid: int, did: int, print: int = 0):
        user = ctx.current_user(req)
        with ehr.session() as c:
            p = load(c, req, user, pid, write=False)
            d = get_disp(c, pid, did)
            meds = c.execute("SELECT * FROM patient_meds WHERE patient_id = ? AND status IN ('taking','prn') ORDER BY name",
                             (pid,)).fetchall()
            audit(c, user, "disposition_print" if print else "disposition_view", entity="patient", entity_id=pid,
                  pnum=p["pnum"], detail=STATUS_LABELS.get(d["kind"], d["kind"]), client=ctx.client(req))
        return render(req, "disposition_print.html" if print else "disposition.html", user, p=p, d=d, meds=meds)

    # ---------------- readmit / correct
    @app.post("/patients/{pid}/readmit")
    async def readmit(req: Request, pid: int):
        user = ctx.current_user(req, roles=("triage", "admin"))
        f = await form(req)
        why, note = f.get("why"), (f.get("note") or "").strip()[:300]
        if why not in ("returned", "error") or len(note) < 5:
            flash(req, "Choose why, and add a short explanation.", "error")
            return RedirectResponse(f"/patients/{pid}", 303)
        with ehr.session() as c:
            p = ctx.get_patient(c, pid)
            if p["status"] in ("active",) or p["incoming"]:
                raise HTTPException(409, "This patient is already in care.")
            t = now()
            last = c.execute("SELECT * FROM dispositions WHERE patient_id = ? AND undone_at IS NULL ORDER BY id DESC LIMIT 1",
                             (pid,)).fetchone()
            if last:
                c.execute("UPDATE dispositions SET undone_at = ?, undone_by = ?, undo_reason = ? WHERE id = ?",
                          (t, user["id"], f"{'Recorded in error' if why == 'error' else 'Returned for care'}: {note}", last["id"]))
            frm = location(c, p["location_id"])
            if why == "error":
                # Put things back the way they were before the disposition.
                prev = c.execute("SELECT to_location_id FROM patient_moves WHERE patient_id = ? AND kind NOT IN ('discharge','death') "
                                 "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
                loc = location(c, prev["to_location_id"]) if prev else None
                loc = loc if loc and loc["active"] and loc["kind"] != "morgue" else location(c, triage_location(c))
                c.execute("UPDATE patients SET status = 'active', disposition_at = NULL, location_id = ?, location_at = ?, "
                          "queued_at = CASE WHEN assigned_to IS NULL THEN ? ELSE NULL END, updated_at = ? WHERE id = ?",
                          (loc["id"] if loc else None, t, t, t, pid))
                record_move(c, pid, "correct", frm=frm["name"] if frm else None, to=loc, status="active", by=user, note=note, at=t)
                audit(c, user, "disposition_correct", entity="patient", entity_id=pid, pnum=p["pnum"],
                      detail=f"was '{STATUS_LABELS.get(p['status'], p['status'])}'; {note}", client=ctx.client(req))
            else:
                # A new visit: back through triage, unassigned, with a fresh triage clock.
                if p["assigned_to"]:
                    c.execute("INSERT INTO assignments(patient_id, at, from_user, to_user, by_user, kind, note) "
                              "VALUES (?,?,?,NULL,?,'unassign',?)", (pid, t, p["assigned_to"], user["id"], "readmitted"))
                c.execute("UPDATE patients SET status = 'active', disposition_at = NULL, assigned_to = NULL, assigned_at = NULL, "
                          "triage_start_at = NULL, triage_done_at = NULL, location_id = NULL, location_at = NULL WHERE id = ?", (pid,))
                enter_triage(c, pid, user, at=t, kind="readmit")
                audit(c, user, "patient_readmit", entity="patient", entity_id=pid, pnum=p["pnum"], detail=note, client=ctx.client(req))
        flash(req, f"Patient {p['pnum']} is back in care." if why == "returned" else "Disposition corrected.")
        return RedirectResponse(f"/patients/{pid}", 303)

    # ---------------- admin: locations (you build the layout: areas, rooms, and beds inside rooms)
    @app.get("/admin/locations", response_class=HTMLResponse)
    async def locations_view(req: Request):
        user = ctx.current_user(req, admin=True)
        with ehr.session() as c:
            locs = all_locations(c, live_only=False)
            occ = occupancy(c)
            used = {r["to_location_id"] for r in c.execute("SELECT DISTINCT to_location_id FROM patient_moves WHERE to_location_id IS NOT NULL")}
            tg = targets(c)
        tops = [l for l in locs if not l["parent_id"]]
        beds: dict[int, list] = {}
        for l in locs:
            if l["parent_id"]:
                beds.setdefault(l["parent_id"], []).append(l)
        has_triage = any(l["kind"] == "triage" and l["active"] and not l["n_children"] for l in locs)
        here: dict[int, int] = {}
        for l in locs:
            here[l["top_id"]] = here.get(l["top_id"], 0) + occ.get(l["id"], 0)
        return render(req, "locations.html", user, tops=tops, beds=beds, occ=occ, here=here, used=used, targets=tg,
                      has_triage=has_triage)

    def back_to(lid=None):
        return RedirectResponse("/admin/locations" + (f"#loc-{lid}" if lid else ""), 303)

    def int_in(v, lo, hi, default):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default

    @app.post("/admin/locations")
    async def location_add(req: Request):
        """Add one location, or several numbered ones ('Bed' x 6 -> Bed 1 ... Bed 6), optionally inside a room."""
        user = ctx.current_user(req, admin=True)
        f = await form(req)
        name = (f.get("name") or "").strip()[:40]
        count = int_in(f.get("count"), 1, 60, 1)
        start = int_in(f.get("start"), 0, 999, 1)
        cap = int_in(f.get("capacity"), 0, 99, 1)
        if not name:
            flash(req, "Give it a name, e.g. 'Room 1', 'Bed', 'Cot', 'Decon tent'.", "error")
            return back_to()
        with ehr.session() as c:
            parent = location(c, int(f["parent_id"])) if (f.get("parent_id") or "").isdigit() else None
            if parent and parent["parent_id"]:
                raise HTTPException(400, "Beds can't have beds inside them.")
            kind = parent["kind"] if parent else f.get("kind")
            if kind not in KIND_LABELS:
                flash(req, "Pick a type.", "error")
                return back_to()
            if parent and occupancy(c).get(parent["id"], 0):
                flash(req, f"{parent['name']} has patients placed in it directly. Move them before adding beds.", "error")
                return back_to(parent["id"])
            names = [name] if count == 1 else [f"{name} {i}" for i in range(start, start + count)]
            pid_ = parent["id"] if parent else None
            clash = [n for n in names if c.execute("SELECT 1 FROM locations WHERE COALESCE(parent_id, 0) = ? AND name = ?",
                                                   (pid_ or 0, n)).fetchone()]
            if clash:
                flash(req, f"Already exists{' in ' + parent['name'] if parent else ''}: {', '.join(clash[:5])}.", "error")
                return back_to(pid_)
            sort = (c.execute("SELECT MAX(sort) AS m FROM locations WHERE COALESCE(parent_id, 0) = ?", (pid_ or 0,)).fetchone()["m"] or 0)
            for n in names:
                sort += 10
                c.execute("INSERT INTO locations(parent_id, name, kind, capacity, sort) VALUES (?,?,?,?,?)",
                          (pid_, n, kind, 1 if parent else cap, sort))
            what = (f"{len(names)} beds in {parent['name']}: {names[0]} - {names[-1]}" if parent and len(names) > 1
                    else f"{names[0]} in {parent['name']}" if parent
                    else f"{', '.join(names) if len(names) <= 3 else names[0] + ' - ' + names[-1]} ({KIND_ONE[kind]})")
            audit(c, user, "location_settings", detail=f"added {what}", client=ctx.client(req))
        flash(req, f"Added {what}.")
        return back_to(pid_)

    @app.post("/admin/locations/sample")
    async def location_sample(req: Request):
        user = ctx.current_user(req, admin=True)
        await form(req)
        from .ehr import seed_locations
        with ehr.session() as c:
            n = seed_locations(c)
            audit(c, user, "location_settings", detail=f"added the sample layout ({n} locations)", client=ctx.client(req))
        flash(req, f"Added {n} sample locations. Rename, remove or add to them to match your space.")
        return back_to()

    @app.post("/admin/locations/{lid}")
    async def location_edit(req: Request, lid: int):
        user = ctx.current_user(req, admin=True)
        f = await form(req)
        with ehr.session() as c:
            old = location(c, lid)
            if not old:
                raise HTTPException(404, "Location not found")
            raw_active = c.execute("SELECT active FROM locations WHERE id = ?", (lid,)).fetchone()["active"]
            name = (f.get("name") or "").strip()[:40]
            kind = old["kind"] if old["parent_id"] else f.get("kind")
            if not name or kind not in KIND_LABELS:
                flash(req, "A name and a type are required.", "error")
                return back_to(lid)
            cap = 1 if old["parent_id"] else int_in(f.get("capacity"), 0, 99, old["capacity"])
            sort = int_in(f.get("sort"), -9999, 99999, old["sort"])
            active = 1 if f.get("active") == "1" else 0
            here = occupancy(c).get(lid, 0) + sum(occupancy(c).get(b["id"], 0) for b in all_locations(c, False) if b["parent_id"] == lid)
            if not active and here:
                flash(req, f"{old['name']} has {here} patient(s) in it. Move them before turning it off.", "error")
                return back_to(lid)
            if kind != old["kind"] and here:
                flash(req, f"{old['name']} has patients in it, so its type can't change right now.", "error")
                return back_to(lid)
            if c.execute("SELECT 1 FROM locations WHERE COALESCE(parent_id, 0) = ? AND name = ? AND id != ?",
                         (old["parent_id"] or 0, name, lid)).fetchone():
                flash(req, f"There is already a '{name}' {'in ' + old['parent_name'] if old['parent_id'] else 'at the top level'}.", "error")
                return back_to(lid)
            c.execute("UPDATE locations SET name = ?, kind = ?, capacity = ?, sort = ?, active = ? WHERE id = ?",
                      (name, kind, cap, sort, active, lid))
            if kind != old["kind"]:
                c.execute("UPDATE locations SET kind = ? WHERE parent_id = ?", (kind, lid))      # beds follow their room
            changes = [f"{k}: {old[k2]} -> {v}" for k, k2, v in (("name", "own_name", name), ("type", "kind", kind),
                                                                  ("capacity", "capacity", cap), ("order", "sort", sort))
                       if str(old[k2]) != str(v)]
            if raw_active != active:
                changes.append("turned on" if active else "turned off")
            audit(c, user, "location_settings", detail=f"{old['name']}: " + (", ".join(changes) or "no changes"), client=ctx.client(req))
        flash(req, f"Saved {name}.")
        return back_to(lid)

    @app.post("/admin/locations/{lid}/delete")
    async def location_delete(req: Request, lid: int):
        """Remove a location that was never used. Used ones are turned off instead so the history keeps their names."""
        user = ctx.current_user(req, admin=True)
        await form(req)
        with ehr.session() as c:
            loc = location(c, lid)
            if not loc:
                raise HTTPException(404, "Location not found")
            ids = [lid] + [r["id"] for r in c.execute("SELECT id FROM locations WHERE parent_id = ?", (lid,))]
            marks = ",".join("?" * len(ids))
            if c.execute(f"SELECT 1 FROM patients WHERE location_id IN ({marks}) AND status IN ('active','deceased')", ids).fetchone():
                flash(req, f"{loc['name']} has patients in it. Move them first.", "error")
                return back_to(lid)
            history = c.execute(f"SELECT 1 FROM patient_moves WHERE to_location_id IN ({marks})", ids).fetchone() or \
                c.execute(f"SELECT 1 FROM patients WHERE location_id IN ({marks})", ids).fetchone()
            if history:
                c.execute(f"UPDATE locations SET active = 0 WHERE id IN ({marks})", ids)
                audit(c, user, "location_settings", detail=f"{loc['name']}: turned off (has patient history, kept for the record)",
                      client=ctx.client(req))
                msg = f"{loc['name']} has patient history, so it was turned off instead of removed."
            else:
                c.execute(f"DELETE FROM locations WHERE parent_id = ?", (lid,))
                c.execute("DELETE FROM locations WHERE id = ?", (lid,))
                audit(c, user, "location_settings", detail=f"removed {loc['name']}" + (f" and its {len(ids) - 1} bed(s)" if len(ids) > 1 else ""),
                      client=ctx.client(req))
                msg = f"Removed {loc['name']}."
        flash(req, msg)
        return back_to(loc["parent_id"])

    @app.post("/admin/locations-targets")
    async def location_targets(req: Request):
        user = ctx.current_user(req, admin=True)
        f = await form(req)
        try:
            warn, alert = int(f.get("warn") or 0), int(f.get("alert") or 0)
        except ValueError:
            warn = alert = -1
        if not (1 <= warn < alert <= 600):
            flash(req, "Use whole minutes, with the warning lower than the alert (e.g. 15 and 30).", "error")
            return RedirectResponse("/admin/locations", 303)
        with ehr.session() as c:
            set_setting(c, "triage_targets", {"warn": warn, "alert": alert})
            audit(c, user, "location_settings", detail=f"triage time targets: amber {warn} min, red {alert} min", client=ctx.client(req))
        flash(req, "Triage time targets saved.")
        return RedirectResponse("/admin/locations", 303)
