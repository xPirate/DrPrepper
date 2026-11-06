"""Touchscreen intake: incoming-patient reports from the kiosk to the triage board.

The touchscreen never opens the EHR. It posts a pre-arrival report over HTTPS with a
device token. The token and a copy of the server's public certificate (for pinning) live
in the kiosk folder, which the touchscreen user can read; that folder holds no patient data.

Reports are write-only: the kiosk gets back only the new patient number.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque
from datetime import datetime, timedelta

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import flow
from .auditlog import audit
from .auth import hash_password, verify_password
from .ehr import get_setting, new_pnum, now, set_setting

AGE_BANDS = {"infant": "infant (<1 y)", "child": "child (1-12 y)", "teen": "teen (13-17 y)", "adult": "adult",
             "older": "older adult (65+)", "unknown": "age unknown"}
SEXES = {"male": "Male", "female": "Female", "unknown": "Unknown"}
CONCERNS = {"overdose": "suspected overdose, substance unknown", "opioid": "suspected opioid overdose",
            "stimulant": "suspected stimulant toxicity", "alcohol": "suspected alcohol intoxication",
            "trauma": "trauma", "medical": "medical illness", "other": "other"}
AVPU = {"alert": "Alert", "voice": "Responds to voice", "pain": "Responds to pain", "unresponsive": "Unresponsive",
        "unknown": "Unknown"}
BREATHING = {"normal": "Normal", "slow": "Slow or shallow", "none": "Not breathing", "unknown": "Unknown"}
NALOXONE = {"no": "No", "yes": "Yes", "unknown": "Unknown"}
DEFAULTS = {"enabled": True, "require_pin": False, "pin_hash": "", "exit_pin_hash": ""}
RATE_LIMIT = (30, 3600)   # reports per window (seconds) per device


class IntakeReport(BaseModel):
    age_band: str = "unknown"
    age: str = Field("", max_length=12)
    sex: str = "unknown"
    concern: str = "overdose"
    avpu: str = "unknown"
    breathing: str = "unknown"
    naloxone: str = "unknown"
    eta_min: int | None = Field(None, ge=0, le=720)
    location: str = Field("", max_length=200)
    notes: str = Field("", max_length=600)
    reporter: str = Field("", max_length=60)
    pin: str = Field("", max_length=12)


# ------------------------------------------------------------------ kiosk folder
def token_path(s):
    return s.kiosk_dir / "token"


def ensure_kiosk_files(s, rotate: bool = False) -> str:
    """Create (or rotate) the device token and refresh the pinned certificate copy."""
    s.kiosk_dir.mkdir(parents=True, exist_ok=True)
    p = token_path(s)
    if rotate or not p.exists():
        tok = secrets.token_urlsafe(32)
        tmp = p.with_name("token.tmp")
        tmp.write_text(tok + "\n")
        tmp.chmod(0o640)
        tmp.replace(p)
    publish_cert(s)
    return p.read_text().strip()


def publish_exit_pin(s, pin_hash: str) -> None:
    """The touchscreen checks its exit PIN locally against this salted hash (the PIN itself is never written)."""
    p = s.kiosk_dir / "exit-pin"
    try:
        if pin_hash:
            tmp = p.with_name("exit-pin.tmp")
            tmp.write_text(pin_hash + "\n")
            tmp.chmod(0o640)
            tmp.replace(p)
        else:
            p.unlink(missing_ok=True)
    except OSError:
        pass


def publish_cert(s) -> None:
    """Copy the public TLS certificate next to the token so the kiosk can pin it."""
    if s.tls_cert and s.tls_cert.exists():
        dst = s.kiosk_dir / "server.crt"
        try:
            s.kiosk_dir.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name("server.crt.tmp")
            tmp.write_bytes(s.tls_cert.read_bytes())
            tmp.chmod(0o644)
            tmp.replace(dst)
        except OSError:
            pass


def token_fingerprint(s) -> str:
    try:
        return hashlib.sha256(token_path(s).read_text().strip().encode()).hexdigest()[:12]
    except OSError:
        return ""


def describe(r: IntakeReport) -> dict:
    """Turn a report into the patient/encounter fields triage sees."""
    age = (r.age or "").strip()
    age_txt = f"~{age.lstrip('~')}" if age else AGE_BANDS.get(r.age_band, "age unknown")
    sex = SEXES.get(r.sex, "Unknown")
    concern = CONCERNS.get(r.concern, "other")
    who = {"Male": "male", "Female": "female"}.get(sex, "person")
    eta_txt = "arriving now" if r.eta_min == 0 else (f"ETA {r.eta_min} min" if r.eta_min else "ETA unknown")
    lines = [
        f"Pre-arrival report received {datetime.now():%H:%M} from the touchscreen"
        + (f" (taken by {r.reporter.strip()})" if r.reporter.strip() else "") + ".",
        f"Patient: {sex}, {age_txt}. Concern: {concern}.",
        f"Responsiveness: {AVPU.get(r.avpu, 'Unknown')}. Breathing: {BREATHING.get(r.breathing, 'Unknown')}. "
        f"Naloxone before arrival: {NALOXONE.get(r.naloxone, 'Unknown')}.",
        f"{eta_txt[0].upper()}{eta_txt[1:]}.",
    ]
    if r.location.strip():
        lines.append(f"Location / unit: {r.location.strip()}")
    if r.notes.strip():
        lines.append(f"Notes: {r.notes.strip()}")
    return {
        "identifying": f"Incoming: unknown {who}, {age_txt}, {concern}",
        "age_est": age_txt, "sex": sex,
        "complaint": f"Pre-arrival: {concern}" + (f" ({AVPU[r.avpu].lower()})" if r.avpu in ("unresponsive", "pain") else ""),
        "history": "\n".join(lines), "eta": eta_txt,
    }


def register(app, ctx) -> None:
    s, ehr, render, flash, form = ctx.s, ctx.ehr, ctx.render, ctx.flash, ctx.form
    recent: deque = deque()
    ensure_kiosk_files(s)

    def kiosk_cfg(c) -> dict:
        cfg = dict(DEFAULTS)
        cfg.update(get_setting(c, "intake", {}) or {})
        return cfg

    def check_token(req: Request) -> None:
        auth = req.headers.get("authorization", "")
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        try:
            good = token_path(s).read_text().strip()
        except OSError:
            good = ""
        if not tok or not good or not hmac.compare_digest(tok, good):
            raise HTTPException(401, "This touchscreen isn't authorised to send reports. Check the kiosk token.")

    @app.get("/api/intake/config")
    async def intake_config(req: Request):
        check_token(req)
        with ehr.session() as c:
            cfg = kiosk_cfg(c)
        return {"enabled": cfg["enabled"], "pin_required": cfg["require_pin"] and bool(cfg["pin_hash"]),
                "age_bands": AGE_BANDS, "concerns": CONCERNS}

    @app.post("/api/intake")
    async def intake(req: Request, r: IntakeReport):
        check_token(req)
        client = ctx.client(req)
        t = time.time()
        while recent and recent[0] < t - RATE_LIMIT[1]:
            recent.popleft()
        reporter = {"id": None, "full_name": "Touchscreen intake" + (f" ({r.reporter.strip()[:40]})" if r.reporter.strip() else "")}
        refusal = None
        with ehr.session() as c:          # record a refusal, then raise after the commit
            cfg = kiosk_cfg(c)
            if not cfg["enabled"]:
                refusal = (403, "Touchscreen intake is turned off. Use the radio or a runner.", "intake is turned off")
            elif cfg["require_pin"] and cfg["pin_hash"] and not await run_in_threadpool(verify_password, r.pin or "", cfg["pin_hash"]):
                refusal = (403, "Wrong intake PIN.", "wrong PIN")
            elif len(recent) >= RATE_LIMIT[0]:
                refusal = (429, "Too many reports from this screen in the last hour.", "too many reports in the last hour")
            if refusal:
                audit(c, reporter, "intake_rejected", detail=refusal[2], client=client)
        if refusal:
            raise HTTPException(refusal[0], refusal[1])
        recent.append(t)
        d = describe(r)
        when = now()
        eta_at = (datetime.fromisoformat(when) + timedelta(minutes=r.eta_min)).isoformat(timespec="seconds") if r.eta_min is not None else None
        with ehr.session() as c:
            pnum = new_pnum(c)
            cur = c.execute("""INSERT INTO patients(pnum, identifying, age_est, sex, allergies, incoming, eta_at, intake_source,
                                                   created_at, updated_at) VALUES (?,?,?,?,?,1,?,?,?,?)""",
                            (pnum, d["identifying"], d["age_est"], d["sex"], "Unknown", eta_at,
                             reporter["full_name"], when, when))
            pid = cur.lastrowid
            c.execute("""INSERT INTO encounters(patient_id, started_at, location, chief_complaint, history)
                         VALUES (?,?,?,?,?)""", (pid, when, r.location.strip() or None, d["complaint"], d["history"]))
            audit(c, reporter, "intake_report", entity="patient", entity_id=pid, pnum=pnum,
                  detail=f"{d['identifying'][10:]}; {d['eta']}", client=client)
        return JSONResponse({"ok": True, "pnum": pnum, "eta_at": eta_at}, status_code=201)

    # ------------------------------------------------------------------ triage actions
    def triage_user(req):
        return ctx.current_user(req, roles=("triage", "admin"))

    @app.post("/patients/{pid}/arrived")
    async def arrived(req: Request, pid: int):
        user = triage_user(req)
        f = await form(req)
        with ehr.session() as c:
            p = ctx.get_patient(c, pid)
            if not p["incoming"]:
                flash(req, f"Patient {p['pnum']} has already arrived.")
                return RedirectResponse(f.get("back") or "/", 303)
            t = now()
            c.execute("UPDATE patients SET incoming = 0, arrived_at = ?, updated_at = ? WHERE id = ?", (t, t, pid))
            flow.enter_triage(c, pid, user, at=t)
            audit(c, user, "patient_arrived", entity="patient", entity_id=pid, pnum=p["pnum"], client=ctx.client(req))
        flash(req, f"Patient {p['pnum']} marked as arrived. Assign them when ready.")
        return RedirectResponse(f.get("back") or "/", 303)

    @app.post("/patients/{pid}/cancel-incoming")
    async def cancel_incoming(req: Request, pid: int):
        user = triage_user(req)
        f = await form(req)
        reason = (f.get("reason") or "did not arrive").strip()[:200]
        with ehr.session() as c:
            p = ctx.get_patient(c, pid)
            c.execute("UPDATE patients SET incoming = 0, status = 'did_not_arrive', disposition_at = ?, updated_at = ? WHERE id = ?",
                      (now(), now(), pid))
            c.execute("UPDATE encounters SET status = 'closed', closed_at = ?, disposition = ? WHERE patient_id = ? AND status = 'open'",
                      (now(), f"Pre-arrival report cancelled: {reason}", pid))
            audit(c, user, "intake_cancel", entity="patient", entity_id=pid, pnum=p["pnum"], detail=reason, client=ctx.client(req))
        flash(req, f"Incoming patient {p['pnum']} cancelled.")
        return RedirectResponse(f.get("back") or "/", 303)

    @app.get("/api/triage/status")
    async def triage_status(req: Request):
        user = ctx.current_user(req, touch=False)
        if user["role"] not in ("triage", "admin"):
            return JSONResponse({"incoming": 0, "latest": 0})
        with ehr.session() as c:
            r = c.execute("SELECT COUNT(*) AS n, MAX(id) AS m FROM patients WHERE incoming = 1").fetchone()
            q = c.execute("SELECT COUNT(*) AS n FROM patients WHERE assigned_to IS NULL AND incoming = 0").fetchone()
        return JSONResponse({"incoming": r["n"], "latest": r["m"] or 0, "queue": q["n"]})

    # ------------------------------------------------------------------ admin settings
    @app.get("/admin/touchscreen", response_class=HTMLResponse)
    async def touch_settings(req: Request):
        user = ctx.current_user(req, admin=True)
        with ehr.session() as c:
            cfg = kiosk_cfg(c)
            reports = c.execute("""SELECT * FROM audit WHERE action IN ('intake_report', 'intake_rejected')
                                   ORDER BY id DESC LIMIT 20""").fetchall()
        return render(req, "touchscreen.html", user, cfg=cfg, fp=token_fingerprint(s), reports=reports,
                      kiosk_dir=str(s.kiosk_dir))

    @app.post("/admin/touchscreen")
    async def touch_save(req: Request):
        user = ctx.current_user(req, admin=True)
        f = await form(req)
        with ehr.session() as c:
            cfg = kiosk_cfg(c)
            cfg["enabled"] = f.get("enabled") == "1"
            cfg["require_pin"] = f.get("require_pin") == "1"
            pin = (f.get("pin") or "").strip()
            if pin:
                if not (pin.isdigit() and 4 <= len(pin) <= 8):
                    flash(req, "The PIN must be 4 to 8 digits.", "error")
                    return RedirectResponse("/admin/touchscreen", 303)
                cfg["pin_hash"] = await run_in_threadpool(hash_password, pin)
            if cfg["require_pin"] and not cfg["pin_hash"]:
                flash(req, "Set a PIN to require one.", "error")
                return RedirectResponse("/admin/touchscreen", 303)
            exit_pin = (f.get("exit_pin") or "").strip()
            exit_note = ""
            if f.get("exit_clear") == "1":
                cfg["exit_pin_hash"], exit_note = "", "; exit PIN removed (Exit to desktop disabled)"
            elif exit_pin:
                if not (exit_pin.isdigit() and 6 <= len(exit_pin) <= 8):
                    flash(req, "The exit PIN must be 6 to 8 digits.", "error")
                    return RedirectResponse("/admin/touchscreen", 303)
                cfg["exit_pin_hash"], exit_note = await run_in_threadpool(hash_password, exit_pin), "; exit PIN changed"
            set_setting(c, "intake", cfg)
            audit(c, user, "intake_settings", detail=f"intake {'on' if cfg['enabled'] else 'off'}, PIN "
                  f"{'required' if cfg['require_pin'] else 'not required'}" + ("; PIN changed" if pin else "") + exit_note,
                  client=ctx.client(req))
        publish_exit_pin(s, cfg["exit_pin_hash"])
        flash(req, "Touchscreen intake settings saved.")
        return RedirectResponse("/admin/touchscreen", 303)

    @app.post("/admin/touchscreen/rotate")
    async def touch_rotate(req: Request):
        user = ctx.current_user(req, admin=True)
        await form(req)
        ensure_kiosk_files(s, rotate=True)
        with ehr.session() as c:
            audit(c, user, "intake_settings", detail=f"device token replaced (new fingerprint {token_fingerprint(s)})",
                  client=ctx.client(req))
        flash(req, "New device token created. The touchscreen on this unit picks it up automatically.")
        return RedirectResponse("/admin/touchscreen", 303)

    def _pub():
        with ehr.session() as c:
            publish_exit_pin(s, kiosk_cfg(c).get("exit_pin_hash", ""))
    ehr.on_ready(_pub)
