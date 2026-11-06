"""Patient history: past medical/surgical/family/social history, code status, and a home medication list.

Home medications are a structured list (one row per drug, optionally linked to the offline drug
database) so they can be checked against medications given here. Rows are never deleted; a wrong
entry is marked "entered in error" and stays in the record.
"""
from __future__ import annotations

import re

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .auditlog import audit
from .ehr import now
from .web import CLINICAL_WRITE

TOBACCO = ["", "Never", "Former", "Current", "Unknown"]
ALCOHOL = ["", "None", "Occasional", "Weekly", "Daily", "Heavy", "Unknown"]
PREGNANCY = ["", "Not applicable", "Not pregnant", "Pregnant", "Possibly pregnant", "Unknown"]
CODE_STATUS = ["", "Full code", "DNR", "DNR/DNI", "Comfort care only", "Unknown"]
LIMITED_CODE = {"DNR", "DNR/DNI", "Comfort care only"}
BLOOD = ["", "A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", "Unknown"]

HISTORY_FIELDS = {
    "conditions": ("Past medical history", 2000), "surgical_hx": ("Surgical history", 2000),
    "family_hx": ("Family history", 1000), "social_tobacco": ("Tobacco", 20), "social_alcohol": ("Alcohol", 20),
    "social_substances": ("Other substance use", 500), "social_notes": ("Social history notes", 1000),
    "pregnancy": ("Pregnancy", 30), "lmp": ("Last menstrual period", 20), "tetanus": ("Last tetanus", 40),
    "immunizations": ("Immunizations", 500), "code_status": ("Code status", 30),
    "advance_directive": ("Advance directive / POLST", 300), "blood_type": ("Blood type", 10),
    "devices": ("Implants and devices", 500), "pcp": ("Usual doctor / clinic", 200),
    "home_meds": ("Medication notes", 1000),
}
CHOICES = {"social_tobacco": TOBACCO, "social_alcohol": ALCOHOL, "pregnancy": PREGNANCY,
           "code_status": CODE_STATUS, "blood_type": BLOOD}

MED_STATUS = {"taking": "Taking", "prn": "As needed", "unknown": "Unsure if still taking", "stopped": "Stopped",
              "error": "Entered in error"}
MED_SOURCES = ["", "Patient", "Family / bystander", "Pill bottle or packaging", "Pharmacy or medical record",
               "EMS / transfer report", "Other"]
MED_FIELDS = ("name", "dose", "route", "frequency", "indication", "last_dose", "source", "notes")


def home_meds(c, pid: int, include_error: bool = False) -> list[dict]:
    q = """SELECT m.*, a.full_name AS added_name, u.full_name AS updated_name FROM patient_meds m
           LEFT JOIN users a ON a.id = m.added_by LEFT JOIN users u ON u.id = m.updated_by
           WHERE m.patient_id = ?""" + ("" if include_error else " AND m.status != 'error'")
    rows = c.execute(q + " ORDER BY CASE m.status WHEN 'taking' THEN 0 WHEN 'prn' THEN 1 WHEN 'unknown' THEN 2 ELSE 3 END, m.name",
                     (pid,)).fetchall()
    return rows


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (s or "").lower())}


def home_med_matches(c, pid: int, drug_id: int | None, name: str, terms: str) -> list[dict]:
    """Current home medications that look like the drug being given (same database entry or shared ingredient name)."""
    want = _words(terms) | _words(name)
    out = []
    for m in home_meds(c, pid):
        if m["status"] not in ("taking", "prn", "unknown"):
            continue
        if (drug_id and m["drug_id"] == drug_id) or (_words(m["name"]) & want):
            out.append(m)
    return out


def register(app, ctx) -> None:
    ehr, render, flash, form = ctx.ehr, ctx.render, ctx.flash, ctx.form
    env = ctx.tpl.env
    env.globals.update(MED_STATUS=MED_STATUS, MED_SOURCES=MED_SOURCES, HISTORY_FIELDS=HISTORY_FIELDS,
                       HISTORY_CHOICES=CHOICES, LIMITED_CODE=LIMITED_CODE)

    def chart(c, req, user, pid, write=True):
        p = ctx.get_patient(c, pid)
        ctx.require_chart(req, user, p, write=write)
        return p

    @app.get("/patients/{pid}/history", response_class=HTMLResponse)
    async def history_form(req: Request, pid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        with ehr.session() as c:
            p = chart(c, req, user, pid)
        return render(req, "history.html", user, p=p)

    @app.post("/patients/{pid}/history")
    async def history_save(req: Request, pid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        v = {}
        for k, (_label, limit) in HISTORY_FIELDS.items():
            val = (f.get(k) or "").strip()[:limit] or None
            if k in CHOICES and val not in CHOICES[k]:
                val = None
            v[k] = val
        with ehr.session() as c:
            old = chart(c, req, user, pid)
            from .ehr import record_revisions
            changed = [HISTORY_FIELDS[k][0] for k in record_revisions(c, "patient", pid, old, v, list(v), user)]
            t = now()
            c.execute(f"UPDATE patients SET {', '.join(f'{k} = ?' for k in v)}, history_updated_at = ?, history_updated_by = ?, "
                      "updated_at = ? WHERE id = ?", (*v.values(), t, user["id"], t, pid))
            ctx.mark_seen(c, pid, user)
            detail = ("changed " + ", ".join(changed)) if changed else "no changes"
            if (old["code_status"] or None) != v["code_status"]:
                detail += f"; code status was '{old['code_status'] or 'not recorded'}', now '{v['code_status'] or 'not recorded'}'"
            audit(c, user, "history_update", entity="patient", entity_id=pid, pnum=old["pnum"], detail=detail, client=ctx.client(req))
        flash(req, "History saved.")
        return RedirectResponse(f"/patients/{pid}#history", 303)

    def med_values(f) -> dict:
        v = {k: ((f.get(k) or "").strip()[:300] or None) for k in MED_FIELDS}
        if not v["name"]:
            raise ValueError("Enter the medication name.")
        if v["source"] and v["source"] not in MED_SOURCES:
            v["source"] = "Other"
        return v

    @app.post("/patients/{pid}/home-meds")
    async def home_med_add(req: Request, pid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        try:
            v = med_values(f)
        except ValueError as e:
            flash(req, str(e), "error")
            return RedirectResponse(f"/patients/{pid}#home-meds", 303)
        status = f.get("status") if f.get("status") in MED_STATUS and f.get("status") != "error" else "taking"
        drug_id = int(f["drug_id"]) if (f.get("drug_id") or "").isdigit() else None
        with ehr.session() as c:
            p = chart(c, req, user, pid)
            c.execute(f"INSERT INTO patient_meds(patient_id, {', '.join(MED_FIELDS)}, drug_id, status, added_at, added_by) "
                      f"VALUES (?, {', '.join('?' * len(MED_FIELDS))}, ?, ?, ?, ?)",
                      (pid, *v.values(), drug_id, status, now(), user["id"]))
            ctx.mark_seen(c, pid, user)
            audit(c, user, "home_med_add", entity="patient", entity_id=pid, pnum=p["pnum"],
                  detail=" ".join(x for x in (v["name"], v["dose"], v["frequency"]) if x) + f" ({MED_STATUS[status].lower()})",
                  client=ctx.client(req))
        return RedirectResponse(f"/patients/{pid}#home-meds", 303)

    @app.post("/patients/{pid}/home-meds/{mid}")
    async def home_med_update(req: Request, pid: int, mid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        with ehr.session() as c:
            p = chart(c, req, user, pid)
            m = c.execute("SELECT * FROM patient_meds WHERE id = ? AND patient_id = ?", (mid, pid)).fetchone()
            if not m:
                raise HTTPException(404, "Medication not found")
            status = f.get("status") if f.get("status") in MED_STATUS else m["status"]
            changes = []
            if status != m["status"]:
                changes.append(f"status {MED_STATUS[m['status']].lower()} -> {MED_STATUS[status].lower()}")
            last = (f.get("last_dose") or "").strip()[:300] or None
            if "last_dose" in f and last != m["last_dose"]:
                changes.append(f"last dose '{m['last_dose'] or ''}' -> '{last or ''}'")
            else:
                last = m["last_dose"]
            if not changes:
                return RedirectResponse(f"/patients/{pid}#home-meds", 303)
            c.execute("UPDATE patient_meds SET status = ?, last_dose = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                      (status, last, now(), user["id"], mid))
            ctx.mark_seen(c, pid, user)
            audit(c, user, "home_med_update", entity="patient", entity_id=pid, pnum=p["pnum"],
                  detail=f"{m['name']}: " + ", ".join(changes), client=ctx.client(req))
        return RedirectResponse(f"/patients/{pid}#home-meds", 303)
