"""FastAPI app: field EHR web console + drug reference, served over the Pi's hotspot."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from ..core.db import LiveDrugDB
from . import admin as admin_routes
from . import intake as intake_routes
from . import branding as branding_routes
from . import board as board_routes
from . import flow
from . import keyseal as keyseal_routes
from . import history as history_routes
from .auditlog import audit
from .auth import Throttle, dummy_verify, hash_password, needs_rehash, new_csrf, password_problem, verify_password
from .config import Settings, ensure_secret
from .ehr import EHR, allergy_hits, allergy_items, allergy_status, new_pnum, now, patient_label, record_revisions, revisions
from .web import CLINICAL_WRITE, HERE, REASONS, Ctx, Redirect


def create_app(settings: Settings | None = None, start_workers: bool = True) -> FastAPI:
    s = settings or Settings.from_env()
    ehr = EHR(s)
    if not ehr.locked:
        ehr.init_schema()
    from . import clock
    drugs = LiveDrugDB(s.drugs_db)   # reopens by itself when a new build replaces drugs.db
    ctx = Ctx(s, ehr, drugs)
    throttle = Throttle()
    dummy_verify("")              # build the decoy hash now, so the first unknown-user login isn't slower
    workers: list = []

    @asynccontextmanager
    async def lifespan(_app):
        def _start():
            with ehr.session() as c:
                audit(c, None, "system_start", detail=f"encryption {'on' if ehr.encrypted else 'OFF'}"
                      + ("; unlocked with passphrase" if ehr.kek else ""))
            if start_workers:
                from .backup import Scheduler
                from .syslog import Forwarder
                workers.extend([Forwarder(ehr), Scheduler(ehr)])
                for w in workers:
                    w.start()
        ehr.on_ready(_start)          # a sealed key: after someone unlocks at /unlock
        yield
        for w in workers:
            w.stop_event.set()

    app = FastAPI(title=s.site_name, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=ensure_secret(s.secret_file), session_cookie="drp_session",
                       max_age=s.session_hours * 3600, same_site="strict", https_only=s.secure_cookies)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    render, flash, form = ctx.render, ctx.flash, ctx.form

    @app.exception_handler(Redirect)
    async def _redir(_req: Request, exc: Redirect):
        return RedirectResponse(exc.url, status_code=303)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if ehr.locked and not LOCKED_OK(request.url.path):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "This unit is locked. Enter the unlock passphrase on the console."}, status_code=503)
            return RedirectResponse("/unlock", 303)
        if ehr.maintenance.is_set() and not request.url.path.startswith("/static"):
            return HTMLResponse("<h1>Restoring from backup</h1><p>The EHR is being restored. Try again in a minute.</p>",
                                status_code=503, headers={"Retry-After": "30"})
        if request.method == "POST":
            try:
                size = int(request.headers.get("content-length") or 0)
            except ValueError:
                size = 0
            if size > upload_limit(request.url.path):
                return HTMLResponse("<h1>Upload too large</h1><p>That file is bigger than this form accepts.</p>", status_code=413)
        resp = await call_next(request)
        if s.secure_cookies:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        if "server" in resp.headers:
            del resp.headers["server"]
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
        if not request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def LOCKED_OK(path: str) -> bool:
        """While the key is sealed: the unlock page and the patient-free drug reference still work."""
        return (path in ("/unlock", "/healthz", "/theme.css", "/trust/drprepper.crt")
                or path.startswith(("/static/", "/drugs", "/tox")) or path == "/branding/icon")

    def upload_limit(path: str) -> int:
        if path.startswith("/admin/backups"):
            return 2 * 1024 ** 3          # backup bundles
        if path.startswith("/admin/trust"):
            return 256 * 1024             # PEM certificates and keys
        return 2 * 1024 ** 2              # forms and icons

    # ------------------------------------------------------------------ small helpers
    def to_int(v, lo=None, hi=None, field=""):
        if v in (None, ""):
            return None
        try:
            n = int(float(v))
        except ValueError:
            raise ValueError(f"{field}: not a number")
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            raise ValueError(f"{field}: {n} is outside {lo}-{hi}")
        return n

    def ts_from_form(v: str | None) -> str:
        if v:
            try:
                return datetime.fromisoformat(v).astimezone().isoformat(timespec="seconds")
            except ValueError:
                pass
        return now()

    # ------------------------------------------------------------------ auth
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(req: Request, next: str = "/"):
        return render(req, "login.html", next=next)

    @app.post("/login")
    async def login(req: Request):
        f = await form(req)
        username = (f.get("username") or "").lower()
        client = ctx.client(req)
        with ehr.session() as c:
            u = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        who = u or {"id": None, "full_name": f"Unknown user '{username[:40]}'"}
        keys = (f"u:{username}|{client}", f"c:{client}")
        if throttle.locked(*keys):
            with ehr.session() as c:
                audit(c, who, "login_locked", detail=f"from {client}", client=client)
            flash(req, "Too many failed attempts. Wait 5 minutes and try again.", "error")
            return RedirectResponse("/login", 303)
        if u:
            ok = await run_in_threadpool(verify_password, f.get("password", ""), u["pw_hash"]) and bool(u["active"])
        else:
            await run_in_threadpool(dummy_verify, f.get("password", ""))      # same cost as a real check
            ok = False
        with ehr.session() as c:
            audit(c, who, "login_ok" if ok else "login_fail",
                  detail=f"from {client}" + ("" if ok or not u or u["active"] else ", account disabled"), client=client)
            if ok:
                c.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), u["id"]))
        if not ok:
            throttle.failure(*keys)
            await asyncio.sleep(throttle.delay(*keys))
            flash(req, "Invalid username or password.", "error")
            return RedirectResponse("/login", 303)
        if needs_rehash(u["pw_hash"]):
            h = await run_in_threadpool(hash_password, f.get("password", ""))
            with ehr.session() as c:
                c.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (h, u["id"]))
        throttle.success(keys[0])
        req.session.clear()
        req.session.update(uid=u["id"], csrf=new_csrf(), grants={})
        with ehr.session() as c:
            ctx.start_session(req, c, u["id"])
        nxt = f.get("next") or "/"
        return RedirectResponse(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/", 303)

    @app.post("/logout")
    async def logout(req: Request):
        await form(req)
        uid = req.session.get("uid")
        if uid:
            with ehr.session() as c:
                u = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                ctx.end_session(req, c, "logout")
                audit(c, u, "logout", client=ctx.client(req))
        req.session.clear()
        return RedirectResponse("/login", 303)

    @app.get("/account/password", response_class=HTMLResponse)
    async def pw_form(req: Request):
        return render(req, "password.html", ctx.current_user(req))

    @app.post("/account/password")
    async def pw_change(req: Request):
        user = ctx.current_user(req)
        f = await form(req)
        with ehr.session() as c:
            row = c.execute("SELECT pw_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not await run_in_threadpool(verify_password, f.get("current", ""), row["pw_hash"]):
            flash(req, "Current password is incorrect.", "error")
        elif f.get("new") != f.get("confirm"):
            flash(req, "The new passwords don't match.", "error")
        elif problem := password_problem(f.get("new", ""), user["username"]):
            flash(req, problem, "error")
        else:
            h = await run_in_threadpool(hash_password, f["new"])
            with ehr.session() as c:
                c.execute("UPDATE users SET pw_hash = ?, must_change_pw = 0 WHERE id = ?", (h, user["id"]))
                n = ctx.revoke_user_sessions(c, user["id"], "password changed", keep_token=req.session.get("sid"))
                audit(c, user, "password_change", detail=f"signed out {n} other session(s)" if n else "", client=ctx.client(req))
            flash(req, "Password updated.")
            return RedirectResponse("/", 303)
        return RedirectResponse("/account/password", 303)

    # ------------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(req: Request):
        user = ctx.current_user(req)
        with ehr.session() as c:
            act = "p.status = 'active'"
            incoming = ctx.patient_rows(c, f"p.incoming = 1 AND {act}", order="COALESCE(p.eta_at, p.created_at)")
            queue = ctx.patient_rows(c, f"p.assigned_to IS NULL AND p.incoming = 0 AND {act}", order="COALESCE(p.queued_at, p.created_at)")
            mine = ctx.patient_rows(c, f"p.assigned_to = ? AND {act}", (user["id"],), order="COALESCE(p.last_seen_at, p.created_at) DESC")
            assigned = ctx.patient_rows(c, f"p.assigned_to IS NOT NULL AND p.incoming = 0 AND {act}", order="a.last_name, p.assigned_at")
            left = ctx.patient_rows(c, "p.status != 'active' AND p.disposition_at >= ?",
                                    (datetime.fromtimestamp(time.time() - 86400).astimezone().isoformat(timespec="seconds"),),
                                    order="p.disposition_at DESC", limit=50)
            people = ctx.assignees(c)
            counts = c.execute("""SELECT (SELECT COUNT(*) FROM patients WHERE status = 'active') AS patients,
                                         (SELECT COUNT(*) FROM patients WHERE assigned_to IS NULL AND incoming = 0 AND status = 'active') AS queue,
                                         (SELECT COUNT(*) FROM patients WHERE incoming = 1 AND status = 'active') AS incoming,
                                         (SELECT COUNT(*) FROM encounters WHERE status = 'open') AS open_enc""").fetchone()
            stats = flow.triage_stats(c)
            tg = flow.targets(c)
            locs = flow.location_options(c)
        board = ctx.can_assign(user)
        return render(req, "dashboard.html", user, incoming=incoming, now_iso=now(), queue=queue, mine=mine, assigned_rows=assigned, people=people,
                      counts=counts, board=board, left=left, stats=stats, targets=tg, locs=locs)

    # ------------------------------------------------------------------ patients
    @app.get("/patients", response_class=HTMLResponse)
    async def patients(req: Request, q: str = "", show: str = "active"):
        user = ctx.current_user(req)
        show = show if show in ("active", "left", "all") else "active"
        status = {"active": "p.status = 'active'", "left": "p.status != 'active'", "all": "1"}[show]
        with ehr.session() as c:
            if q:
                like = f"%{q}%"
                rows = ctx.patient_rows(c, f"({status}) AND (p.last_name LIKE ? OR p.first_name LIKE ? OR p.pnum LIKE ? OR p.identifying LIKE ?)",
                                        (like, like, like, like), order="p.last_name, p.first_name")
                audit(c, user, "patient_search", detail=f"'{q[:60]}', {len(rows)} result(s)", client=ctx.client(req))
            else:
                rows = ctx.patient_rows(c, status)
        return render(req, "patients.html", user, rows=rows, q=q, show=show)

    PATIENT_FIELDS = ("last_name", "first_name", "dob", "age_est", "sex", "weight_kg", "allergies",
                      "conditions", "home_meds", "contact", "identifying")

    def patient_values(f: dict) -> dict:
        v = {k: f.get(k) or None for k in PATIENT_FIELDS}
        if v["weight_kg"]:
            try:
                w = float(v["weight_kg"])
            except ValueError:
                raise ValueError("Weight must be a number")
            if f.get("weight_unit") == "lb":
                w = w * 0.45359237
            if not 0.3 <= w <= 400:
                raise ValueError("Weight must be 0.3-400 kg")
            v["weight_kg"] = round(w, 1)
        if not (v["last_name"] or v["first_name"] or v["identifying"]):
            raise ValueError("Enter a name, or an identifying description for an unknown patient (e.g. 'Unknown male #3, red jacket').")
        return v

    @app.get("/patients/new", response_class=HTMLResponse)
    async def patient_new_form(req: Request):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        with ehr.session() as c:
            people = ctx.assignees(c)
        return render(req, "patient_form.html", user, p={}, action="/patients/new", people=people)

    @app.post("/patients/new")
    async def patient_new(req: Request):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        try:
            v = patient_values(f)
        except ValueError as e:
            flash(req, str(e), "error")
            with ehr.session() as c:
                people = ctx.assignees(c)
            return render(req, "patient_form.html", user, p=f, action="/patients/new", people=people)
        with ehr.session() as c:
            pnum = new_pnum(c)
            # Providers own what they create; triage/admin can pick someone or leave it in the queue.
            to = user["id"] if user["role"] == "provider" else (int(f["assign_to"]) if f.get("assign_to") else None)
            t = now()
            cur = c.execute(
                f"INSERT INTO patients(pnum, {', '.join(PATIENT_FIELDS)}, assigned_to, assigned_at, created_at, created_by) "
                f"VALUES (?, {', '.join('?' * len(PATIENT_FIELDS))}, ?, ?, ?, ?)",
                (pnum, *v.values(), to, t if to else None, t, user["id"]))
            pid = cur.lastrowid
            audit(c, user, "patient_create", entity="patient", entity_id=pid, pnum=pnum, client=ctx.client(req))
            flow.enter_triage(c, pid, user, at=t)
            if to:
                target = c.execute("SELECT * FROM users WHERE id = ?", (to,)).fetchone()
                flow.on_assigned(c, pid, t)
                c.execute("INSERT INTO assignments(patient_id, at, to_user, by_user, kind, note) VALUES (?,?,?,?, 'assign', ?)",
                          (pid, t, to, user["id"], "assigned at registration; 0 min in triage"))
                audit(c, user, "patient_assign", entity="patient", entity_id=pid, pnum=pnum, target=target["full_name"])
        ctx.grant(req, pid)  # the creator can always open what they just made
        flash(req, f"Patient {pnum} created.")
        return RedirectResponse(f"/patients/{pid}", 303)

    @app.get("/patients/{pid}/access", response_class=HTMLResponse)
    async def access_form(req: Request, pid: int, next: str = ""):
        user = ctx.current_user(req)
        with ehr.session() as c:
            p = ctx.get_patient(c, pid)
        if ctx.access(req, user, p):
            return RedirectResponse(next or f"/patients/{pid}", 303)
        return render(req, "access.html", user, p=p, reasons=REASONS, next=next, minutes=s.grant_minutes)

    @app.post("/patients/{pid}/access")
    async def access_grant(req: Request, pid: int):
        user = ctx.current_user(req)
        f = await form(req)
        reason, note = f.get("reason", ""), (f.get("note") or "").strip()
        with ehr.session() as c:
            p = ctx.get_patient(c, pid)
        if reason not in REASONS or (reason.startswith("Other") and len(note) < 5):
            flash(req, "Choose a reason. For 'Other', describe why you need this chart.", "error")
            return RedirectResponse(f"/patients/{pid}/access?next={f.get('next', '')}", 303)
        full_reason = reason.replace(" (explain below)", "") + (f" - {note[:300]}" if note else "")
        with ehr.session() as c:
            audit(c, user, "patient_view_justified", entity="patient", entity_id=pid, pnum=p["pnum"], reason=full_reason,
                  client=ctx.client(req))
        ctx.grant(req, pid)
        nxt = f.get("next") or f"/patients/{pid}"
        return RedirectResponse(nxt if nxt.startswith(f"/patients/{pid}") or nxt.startswith("/encounters/") else f"/patients/{pid}", 303)

    @app.get("/patients/{pid}", response_class=HTMLResponse)
    async def patient_view(req: Request, pid: int):
        user = ctx.current_user(req)
        with ehr.session() as c:
            p = ctx.get_patient(c, pid)
            access = ctx.require_chart(req, user, p)
            encs = c.execute("""SELECT e.*, u.full_name AS provider FROM encounters e LEFT JOIN users u ON u.id = e.provider_id
                                WHERE patient_id = ? ORDER BY started_at DESC""", (pid,)).fetchall()
            history = c.execute("""SELECT a.*, f.full_name AS from_name, t.full_name AS to_name, b.full_name AS by_name
                                   FROM assignments a LEFT JOIN users f ON f.id = a.from_user LEFT JOIN users t ON t.id = a.to_user
                                   LEFT JOIN users b ON b.id = a.by_user WHERE a.patient_id = ? ORDER BY a.at DESC""", (pid,)).fetchall()
            people = ctx.assignees(c)
            fx = dict(
                moves=c.execute("""SELECT m.*, u.full_name AS by_name FROM patient_moves m LEFT JOIN users u ON u.id = m.by_user
                                   WHERE m.patient_id = ? ORDER BY m.id DESC""", (pid,)).fetchall(),
                disps=c.execute("""SELECT d.*, u.full_name AS by_name FROM dispositions d LEFT JOIN users u ON u.id = d.by_user
                                   WHERE d.patient_id = ? ORDER BY d.id DESC""", (pid,)).fetchall(),
                locs=flow.location_options(c), targets=flow.targets(c), can_move=flow.can_move(ctx, user, p),
                hmeds=history_routes.home_meds(c, pid), now_iso=now(), revs=revisions(c, "patient", pid),
            )
            audit(c, user, "patient_view", entity="patient", entity_id=pid, pnum=p["pnum"],
                  detail="justified access" if access == "justified" else "", client=ctx.client(req))
        return render(req, "patient.html", user, p=p, encs=encs, history=history, people=people, access=access,
                      can_assign=ctx.can_assign(user), can_handoff=ctx.can_handoff(user, p), **fx)

    @app.get("/patients/{pid}/edit", response_class=HTMLResponse)
    async def patient_edit_form(req: Request, pid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        with ehr.session() as c:
            p = ctx.get_patient(c, pid)
            ctx.require_chart(req, user, p, write=True)
        return render(req, "patient_form.html", user, p=p, action=f"/patients/{pid}/edit", people=[])

    @app.post("/patients/{pid}/edit")
    async def patient_edit(req: Request, pid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        try:
            v = patient_values(f)
        except ValueError as e:
            flash(req, str(e), "error")
            return RedirectResponse(f"/patients/{pid}/edit", 303)
        with ehr.session() as c:
            old = ctx.get_patient(c, pid)
            ctx.require_chart(req, user, old, write=True)
            changed = record_revisions(c, "patient", pid, old, v, PATIENT_FIELDS, user)
            c.execute(f"UPDATE patients SET {', '.join(f'{k} = ?' for k in PATIENT_FIELDS)}, updated_at = ? WHERE id = ?",
                      (*v.values(), now(), pid))
            detail = "changed " + ", ".join(changed) if changed else "no changes"
            if "allergies" in changed:
                detail += f"; allergies were '{old['allergies'] or ''}', now '{v['allergies'] or ''}'"
            audit(c, user, "patient_update", entity="patient", entity_id=pid, pnum=old["pnum"], detail=detail,
                  client=ctx.client(req))
        flash(req, "Patient updated.")
        return RedirectResponse(f"/patients/{pid}", 303)

    # ------------------------------------------------------------------ allergies (never behind chart access)
    @app.post("/api/patients/{pid}/allergies")
    async def allergy_popup(req: Request, pid: int):
        user = ctx.current_user(req)
        ctx.check_csrf_header(req)
        with ehr.session() as c:
            p = c.execute("SELECT id, pnum, last_name, first_name, identifying, allergies FROM patients WHERE id = ?", (pid,)).fetchone()
            if not p:
                raise HTTPException(404, "Patient not found")
            audit(c, user, "allergy_view", entity="patient", entity_id=pid, pnum=p["pnum"], client=ctx.client(req))
        return JSONResponse({"pnum": p["pnum"], "name": patient_label(p), "status": allergy_status(p["allergies"]),
                             "items": allergy_items(p["allergies"])})

    # ------------------------------------------------------------------ assignment
    @app.post("/patients/{pid}/assign")
    async def assign(req: Request, pid: int):
        user = ctx.current_user(req)
        f = await form(req)
        to = int(f["to_user"]) if f.get("to_user") else None
        note = (f.get("note") or "").strip()[:300]
        back = f.get("back") or f"/patients/{pid}"
        with ehr.session() as c:
            p = ctx.get_patient(c, pid)
            if not ctx.can_handoff(user, p):
                raise HTTPException(403, "Only triage, or the patient's assigned provider, can reassign this patient.")
            if to is None and not ctx.can_assign(user):
                raise HTTPException(403, "Only triage can return a patient to the queue.")
            if p["status"] != "active":
                raise HTTPException(409, "This patient is no longer in care. Readmit them first.")
            target = None
            if to is not None:
                target = c.execute("SELECT * FROM users WHERE id = ? AND active = 1 AND role IN ('provider','triage')", (to,)).fetchone()
                if not target:
                    raise HTTPException(400, "Pick an active provider.")
                if to == p["assigned_to"]:
                    flash(req, f"Already assigned to {target['full_name']}.")
                    return RedirectResponse(back, 303)
            if to is None:
                kind = "unassign"
            elif p["assigned_to"] == user["id"] or not ctx.can_assign(user):
                kind = "handoff"          # the assigned provider passing the patient on (end of shift)
            else:
                kind = "assign"           # triage assigning or reassigning
            t = now()
            c.execute("UPDATE patients SET assigned_to = ?, assigned_at = ? WHERE id = ?", (to, t if to else None, pid))
            tri_min = flow.on_assigned(c, pid, t) if to else flow.on_unassigned(c, pid, t)
            tri_part = f"after {flow.fmt_minutes(tri_min)} in triage" if tri_min is not None else ""
            note_db = "; ".join(x for x in (tri_part, note) if x) or None
            detail = "; ".join(x for x in (tri_part, f"note: {note}" if note else "") if x)
            c.execute("INSERT INTO assignments(patient_id, at, from_user, to_user, by_user, kind, note) VALUES (?,?,?,?,?,?,?)",
                      (pid, t, p["assigned_to"], to, user["id"], kind, note_db))
            action = {"unassign": "patient_unassign", "handoff": "patient_handoff", "assign": "patient_assign"}[kind]
            audit(c, user, action, entity="patient", entity_id=pid, pnum=p["pnum"],
                  target=target["full_name"] if target else None, detail=detail, client=ctx.client(req))
        if kind == "handoff":
            flash(req, f"Handed off to {target['full_name']}.")
        elif kind == "unassign":
            flash(req, f"Patient {p['pnum']} returned to the triage queue.")
        else:
            flash(req, f"Patient {p['pnum']} assigned to {target['full_name']}.")
        return RedirectResponse(back, 303)

    # ------------------------------------------------------------------ encounters
    @app.post("/patients/{pid}/encounters")
    async def encounter_start(req: Request, pid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        with ehr.session() as c:
            p = ctx.get_patient(c, pid)
            ctx.require_chart(req, user, p, write=True)
            cur = c.execute("""INSERT INTO encounters(patient_id, started_at, location, chief_complaint, provider_id)
                               VALUES (?,?,?,?,?)""",
                            (pid, ts_from_form(f.get("started_at")), f.get("location"), f.get("chief_complaint"), user["id"]))
            eid = cur.lastrowid
            ctx.mark_seen(c, pid, user)
            audit(c, user, "encounter_open", entity="encounter", entity_id=eid, eid=eid, pnum=p["pnum"], client=ctx.client(req))
        return RedirectResponse(f"/encounters/{eid}", 303)

    def encounter_bundle(c, eid: int) -> dict:
        e = ctx.get_encounter(c, eid)
        return dict(
            e=e,
            p=ctx.get_patient(c, e["patient_id"]),
            provider=c.execute("SELECT full_name FROM users WHERE id = ?", (e["provider_id"],)).fetchone(),
            vitals=c.execute("""SELECT v.*, u.full_name AS by_name FROM vitals v LEFT JOIN users u ON u.id = v.recorded_by
                                WHERE encounter_id = ? ORDER BY taken_at""", (eid,)).fetchall(),
            meds=c.execute("""SELECT m.*, u.full_name AS by_name FROM med_admin m LEFT JOIN users u ON u.id = m.given_by
                              WHERE encounter_id = ? ORDER BY given_at""", (eid,)).fetchall(),
            notes=c.execute("""SELECT n.*, u.full_name AS by_name FROM notes n LEFT JOIN users u ON u.id = n.author_id
                               WHERE encounter_id = ? ORDER BY created_at""", (eid,)).fetchall(),
            hmeds=history_routes.home_meds(c, e["patient_id"]),
            revs=revisions(c, "encounter", eid),
        )

    def open_encounter(req, c, user, eid: int) -> tuple[dict, dict]:
        e = ctx.get_encounter(c, eid)
        p = ctx.get_patient(c, e["patient_id"])
        ctx.require_chart(req, user, p, write=True)
        if e["status"] != "open":
            raise HTTPException(409, "This encounter is closed. Reopen it to add entries.")
        return e, p

    def drug_terms(drug_id: int | None, name: str) -> str:
        parts = [name]
        if drugs and drug_id:
            d = drugs.get_drug(drug_id)
            if d:
                parts += [d["generic"], d["brands"] or "", d["substances"] or "", d["pharm_class"] or ""]
        return " | ".join(parts)

    @app.get("/encounters/{eid}", response_class=HTMLResponse)
    async def encounter_view(req: Request, eid: int):
        user = ctx.current_user(req)
        with ehr.session() as c:
            b = encounter_bundle(c, eid)
            access = ctx.require_chart(req, user, b["p"])
            audit(c, user, "encounter_view", entity="encounter", entity_id=eid, eid=eid, pnum=b["p"]["pnum"],
                  detail="justified access" if access == "justified" else "", client=ctx.client(req))
        pending = req.session.pop("pending_med", None)
        return render(req, "encounter.html", user, pending=pending, access=access, **b)

    @app.get("/encounters/{eid}/print", response_class=HTMLResponse)
    async def encounter_print(req: Request, eid: int):
        user = ctx.current_user(req)
        with ehr.session() as c:
            b = encounter_bundle(c, eid)
            ctx.require_chart(req, user, b["p"])
            audit(c, user, "encounter_print", entity="encounter", entity_id=eid, eid=eid, pnum=b["p"]["pnum"], client=ctx.client(req))
        return render(req, "encounter_print.html", user, **b)

    @app.post("/encounters/{eid}/update")
    async def encounter_update(req: Request, eid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        fields = ("location", "chief_complaint", "history", "exam", "assessment", "plan", "disposition")
        with ehr.session() as c:
            e, p = open_encounter(req, c, user, eid)
            changed = record_revisions(c, "encounter", eid, e, {k: f.get(k) or None for k in fields}, fields, user)
            c.execute(f"UPDATE encounters SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                      (*(f.get(k) or None for k in fields), eid))
            ctx.mark_seen(c, p["id"], user)
            audit(c, user, "encounter_update", entity="encounter", entity_id=eid, eid=eid, pnum=p["pnum"],
                  detail=("changed " + ", ".join(changed)) if changed else "", client=ctx.client(req))
        flash(req, "Encounter saved.")
        return RedirectResponse(f"/encounters/{eid}", 303)

    @app.post("/encounters/{eid}/vitals")
    async def vitals_add(req: Request, eid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        try:
            temp = f.get("temp") or None
            if temp:
                t = float(temp)
                if f.get("temp_unit") == "F":
                    t = (t - 32) * 5 / 9
                if not 20 <= t <= 46:
                    raise ValueError("Temperature: outside 20-46 C")
                temp = round(t, 1)
            v = dict(
                hr=to_int(f.get("hr"), 0, 300, "HR"), bp_sys=to_int(f.get("bp_sys"), 0, 300, "Systolic BP"),
                bp_dia=to_int(f.get("bp_dia"), 0, 200, "Diastolic BP"), rr=to_int(f.get("rr"), 0, 80, "RR"),
                spo2=to_int(f.get("spo2"), 0, 100, "SpO2"), temp_c=temp, gcs=to_int(f.get("gcs"), 3, 15, "GCS"),
                glucose=to_int(f.get("glucose"), 0, 2000, "Glucose"), pain=to_int(f.get("pain"), 0, 10, "Pain"),
            )
        except ValueError as e:
            flash(req, str(e), "error")
            return RedirectResponse(f"/encounters/{eid}#vitals", 303)
        if all(x is None for x in v.values()) and not f.get("notes"):
            flash(req, "Enter at least one vital sign.", "error")
            return RedirectResponse(f"/encounters/{eid}#vitals", 303)
        with ehr.session() as c:
            _e, p = open_encounter(req, c, user, eid)
            c.execute(f"""INSERT INTO vitals(encounter_id, taken_at, {', '.join(v)}, notes, recorded_by)
                          VALUES (?, ?, {', '.join('?' * len(v))}, ?, ?)""",
                      (eid, ts_from_form(f.get("taken_at")), *v.values(), f.get("notes") or None, user["id"]))
            ctx.mark_seen(c, p["id"], user)
            audit(c, user, "vitals_add", entity="encounter", entity_id=eid, eid=eid, pnum=p["pnum"], client=ctx.client(req))
        return RedirectResponse(f"/encounters/{eid}#vitals", 303)

    @app.post("/encounters/{eid}/meds")
    async def med_add(req: Request, eid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        name, dose, route = f.get("drug_name"), f.get("dose"), f.get("route")
        if not (name and dose and route):
            flash(req, "Drug, dose and route are required.", "error")
            req.session["pending_med"] = {k: v for k, v in f.items() if k != "csrf"}
            return RedirectResponse(f"/encounters/{eid}#meds", 303)
        drug_id = to_int(f.get("drug_id")) if f.get("drug_id") else None
        with ehr.session() as c:
            _e, p = open_encounter(req, c, user, eid)
            hits = allergy_hits(p["allergies"], drug_terms(drug_id, name))
            if hits and f.get("override") != "1":
                pend = {k: v for k, v in f.items() if k != "csrf"}
                pend["allergy_hits"] = hits
                req.session["pending_med"] = pend
                audit(c, user, "allergy_alert", entity="encounter", entity_id=eid, eid=eid, pnum=p["pnum"],
                      detail=f"{name} vs {'; '.join(hits)}", client=ctx.client(req))
                flash(req, f"ALLERGY ALERT: patient lists {', '.join(hits)}. Review before giving {name}.", "error")
                return RedirectResponse(f"/encounters/{eid}#meds", 303)
            cur = c.execute("""INSERT INTO med_admin(encounter_id, given_at, drug_id, drug_name, dose, route,
                                                     indication, response, allergy_override, given_by)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (eid, ts_from_form(f.get("given_at")), drug_id, name, dose, route,
                             f.get("indication") or None, f.get("response") or None, 1 if hits else 0, user["id"]))
            ctx.mark_seen(c, p["id"], user)
            dup = history_routes.home_med_matches(c, p["id"], drug_id, name, drug_terms(drug_id, name))
            audit(c, user, "med_override" if hits else "med_given", entity="med_admin", entity_id=cur.lastrowid, pnum=p["pnum"],
                  detail=f"{name} {dose} {route}" + (f" (allergy: {'; '.join(hits)})" if hits else ""), client=ctx.client(req))
        if dup:
            flash(req, f"{name} recorded. Heads up: the patient's home medications include "
                       + "; ".join(m["name"] + (f" {m['dose']}" if m["dose"] else "") + (f", last dose {m['last_dose']}" if m["last_dose"] else "")
                                   for m in dup) + ". Check for doubling up.", "error")
        return RedirectResponse(f"/encounters/{eid}#meds", 303)

    @app.post("/encounters/{eid}/meds/{mid}/response")
    async def med_response(req: Request, eid: int, mid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        with ehr.session() as c:
            _e, p = open_encounter(req, c, user, eid)
            c.execute("UPDATE med_admin SET response = ? WHERE id = ? AND encounter_id = ?", (f.get("response") or None, mid, eid))
            ctx.mark_seen(c, p["id"], user)
            audit(c, user, "med_response", entity="med_admin", entity_id=mid, pnum=p["pnum"], client=ctx.client(req))
        return RedirectResponse(f"/encounters/{eid}#meds", 303)

    @app.post("/encounters/{eid}/notes")
    async def note_add(req: Request, eid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        if not f.get("body"):
            return RedirectResponse(f"/encounters/{eid}#notes", 303)
        with ehr.session() as c:
            _e, p = open_encounter(req, c, user, eid)
            cur = c.execute("INSERT INTO notes(encounter_id, created_at, author_id, body) VALUES (?,?,?,?)",
                            (eid, now(), user["id"], f["body"]))
            ctx.mark_seen(c, p["id"], user)
            audit(c, user, "note_add", entity="note", entity_id=cur.lastrowid, pnum=p["pnum"], client=ctx.client(req))
        return RedirectResponse(f"/encounters/{eid}#notes", 303)

    @app.post("/encounters/{eid}/close")
    async def encounter_close(req: Request, eid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        f = await form(req)
        with ehr.session() as c:
            _e, p = open_encounter(req, c, user, eid)
            c.execute("UPDATE encounters SET status = 'closed', closed_at = ?, disposition = COALESCE(?, disposition) WHERE id = ?",
                      (now(), f.get("disposition") or None, eid))
            ctx.mark_seen(c, p["id"], user)
            audit(c, user, "encounter_close", entity="encounter", entity_id=eid, eid=eid, pnum=p["pnum"], client=ctx.client(req))
        flash(req, "Encounter closed.")
        return RedirectResponse(f"/encounters/{eid}", 303)

    @app.post("/encounters/{eid}/reopen")
    async def encounter_reopen(req: Request, eid: int):
        user = ctx.current_user(req, roles=CLINICAL_WRITE)
        await form(req)
        with ehr.session() as c:
            e = ctx.get_encounter(c, eid)
            p = ctx.get_patient(c, e["patient_id"])
            ctx.require_chart(req, user, p, write=True)
            c.execute("UPDATE encounters SET status = 'open', closed_at = NULL WHERE id = ?", (eid,))
            audit(c, user, "encounter_reopen", entity="encounter", entity_id=eid, eid=eid, pnum=p["pnum"], client=ctx.client(req))
        flash(req, "Encounter reopened.")
        return RedirectResponse(f"/encounters/{eid}", 303)

    # ------------------------------------------------------------------ drug reference
    def ref_user(req: Request) -> dict | None:
        if ehr.locked or (s.public_reference and not req.session.get("uid")):
            return None
        return ctx.current_user(req)

    def need_drugs():
        if not drugs:
            raise HTTPException(503, f"Drug database not found at {s.drugs_db}. Run drprepper-ingest.")
        return drugs

    @app.get("/drugs", response_class=HTMLResponse)
    async def drug_search(req: Request, q: str = "", kind: str = ""):
        user = ref_user(req)
        res = need_drugs().search(q, kind=kind or None) if q else None
        return render(req, "drugs.html", user, q=q, kind=kind, res=res)

    @app.get("/drugs/{drug_id}", response_class=HTMLResponse)
    async def drug_view(req: Request, drug_id: int):
        user = ref_user(req)
        d = need_drugs().get_drug(drug_id)
        if not d:
            raise HTTPException(404, "Drug not found")
        return render(req, "drug.html", user, d=d)

    @app.get("/tox", response_class=HTMLResponse)
    async def tox_index(req: Request):
        user = ref_user(req)
        groups: dict[str, list] = {}
        for t in need_drugs().list_tox():
            groups.setdefault(t.category, []).append(t)
        return render(req, "tox_index.html", user, groups=groups)

    @app.get("/tox/{slug}", response_class=HTMLResponse)
    async def tox_view(req: Request, slug: str):
        user = ref_user(req)
        t = need_drugs().get_tox(slug)
        if not t:
            raise HTTPException(404, "Card not found")
        return render(req, "tox.html", user, t=t)

    @app.get("/api/drugs")
    async def api_drugs(req: Request, q: str = "", limit: int = 10, prefer: str = ""):
        ctx.current_user(req)
        if not drugs or len(q.strip()) < 2:
            return JSONResponse([])
        hits = drugs.suggest(q, limit=40)
        seen, out = set(), []
        for h in hits:                       # one entry per name + route (labels repeat across manufacturers)
            key = (" ".join(h.display.lower().split()), (h.route or "").lower())
            if key not in seen:
                seen.add(key)
                out.append(h)
        if prefer:                           # e.g. home medications: oral forms first
            out.sort(key=lambda h: 0 if prefer.lower() in (h.route or "").lower() else 1)
        return JSONResponse([{"id": h.id, "name": h.display, "route": h.route, "kind": h.kind, "brands": h.brands[:80]}
                             for h in out[:min(limit, 25)]])

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "locked": ehr.locked, "drugs": bool(drugs), "drug_data": "sample" if drugs.is_sample else "full"}

    # Public: devices download this to trust the unit's certificate.
    @app.get("/trust/drprepper.crt")
    async def public_cert():
        if not s.tls_cert.exists():
            raise HTTPException(404, "No certificate installed")
        return Response(s.tls_cert.read_bytes(), media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="drprepper.crt"'})

    keyseal_routes.register(app, ctx)
    admin_routes.register(app, ctx)
    intake_routes.register(app, ctx)
    branding_routes.register(app, ctx)
    clock.register(app, ctx)
    flow.register(app, ctx)
    board_routes.register(app, ctx)
    history_routes.register(app, ctx)

    # ------------------------------------------------------------------ errors
    @app.exception_handler(HTTPException)
    async def http_error(req: Request, exc: HTTPException):
        if req.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return render(req, "error.html", {"id": None, "full_name": "", "role": ""} if req.session.get("uid") else None,
                      status_code=exc.status_code, status=exc.status_code, detail=exc.detail)

    app.state.ehr, app.state.drugs, app.state.ctx = ehr, drugs, ctx
    return app


def app_factory() -> FastAPI:  # uvicorn --factory drprepper.server.app:app_factory
    return create_app()
