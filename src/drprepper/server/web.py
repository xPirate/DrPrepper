"""Shared request helpers: sessions, roles, chart access, rendering."""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..core.text import chunk_text
from .auditlog import audit
from .auth import new_csrf
from .ehr import ROLE_LABELS, allergy_items, allergy_status, now, patient_label

HERE = Path(__file__).parent
CLINICAL_WRITE = ("admin", "triage", "provider")
ASSIGNABLE = ("provider", "triage")
REASONS = [
    "Covering for the assigned provider",
    "Emergency: patient deteriorating",
    "Giving or checking a medication",
    "Handoff / transfer of care",
    "Triage reassessment",
    "Quality or safety review",
    "Other (explain below)",
]


def _sid_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def fmt_dt(v) -> str:
    """Stored timestamp shown in the unit's time zone (older entries written in another zone are converted)."""
    from .clock import to_local
    d = to_local(v)
    return d.strftime("%Y-%m-%d %H:%M") if d else (str(v or ""))[:16].replace("T", " ")


def fmt_hm(v) -> str:
    from .clock import to_local
    d = to_local(v)
    return d.strftime("%H:%M") if d else ""


class Ctx:
    """Everything a route needs, shared by the clinical and admin route modules."""

    def __init__(self, settings, ehr, drugs):
        self.s, self.ehr, self.drugs = settings, ehr, drugs
        from .branding import Brand
        self.brand = Brand(self)
        self.tpl = Jinja2Templates(directory=HERE / "templates")
        f = self.tpl.env.filters
        f["dt"] = fmt_dt
        f["hm"] = fmt_hm
        f["localiso"] = lambda v: (lambda d: d.isoformat(timespec="seconds") if d else "")(__import__("drprepper.server.clock", fromlist=["x"]).to_local(v))
        f["chunks"] = chunk_text
        f["allergy_status"] = allergy_status
        f["allergy_items"] = allergy_items
        f["plabel"] = patient_label
        f["rolelabel"] = lambda r: ROLE_LABELS.get(r, r)
        self.tpl.env.globals["ROLE_LABELS"] = ROLE_LABELS

    @property
    def drug_meta(self) -> dict:
        return self.drugs.meta() if self.drugs else {}

    def drug_data_state(self) -> dict | None:
        """None when the full FDA database is installed; otherwise what the sample banner should say."""
        if self.drugs and not self.drugs.is_sample:
            return None
        st = self.drugs.ingest_status() if self.drugs is not None else None
        return {"installed": bool(self.drugs), "status": st}

    # ---------------- session / user
    def flash(self, req: Request, msg: str, kind: str = "info") -> None:
        # Reassign rather than mutate: the session middleware only saves the cookie when a key is set.
        req.session["flash"] = [*req.session.get("flash", []), [kind, msg]]

    def client(self, req: Request) -> str:
        return req.client.host if req.client else "?"

    # ---------------- server-side sessions: the cookie carries a random token; the server decides if it's still good
    def start_session(self, req: Request, c, user_id: int) -> None:
        token = secrets.token_urlsafe(32)
        t = now()
        old = datetime.fromtimestamp(time.time() - 30 * 86400).astimezone().isoformat(timespec="seconds")
        c.execute("DELETE FROM sessions WHERE last_seen_at < ?", (old,))      # keep the table small
        c.execute("INSERT INTO sessions(id, user_id, created_at, last_seen_at, client) VALUES (?,?,?,?,?)",
                  (_sid_hash(token), user_id, t, t, self.client(req)))
        req.session["sid"] = token

    def end_session(self, req: Request, c, reason: str = "logout") -> None:
        token = req.session.get("sid")
        if token:
            c.execute("UPDATE sessions SET revoked_at = ?, revoked_reason = ? WHERE id = ? AND revoked_at IS NULL",
                      (now(), reason, _sid_hash(token)))

    @staticmethod
    def revoke_user_sessions(c, user_id: int, reason: str, keep_token: str | None = None) -> int:
        keep = _sid_hash(keep_token) if keep_token else ""
        cur = c.execute("UPDATE sessions SET revoked_at = ?, revoked_reason = ? WHERE user_id = ? AND revoked_at IS NULL AND id != ?",
                        (now(), reason, user_id, keep))
        return cur.rowcount

    def current_user(self, req: Request, *, roles: tuple[str, ...] | None = None, admin: bool = False,
                     touch: bool = True) -> dict:
        uid, token = req.session.get("uid"), req.session.get("sid")
        if not uid or not token:
            req.session.pop("uid", None)
            raise Redirect(f"/login?next={req.url.path}")
        sid = _sid_hash(token)
        out = None                         # decided inside the transaction, acted on after it commits
        with self.ehr.session() as c:
            srow = c.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
            if not srow or srow["revoked_at"] or srow["user_id"] != uid:
                out = ("gone", srow["revoked_reason"] if srow else None)
            else:
                idle = time.time() - datetime.fromisoformat(srow["last_seen_at"]).timestamp()
                u = c.execute("SELECT id, username, full_name, last_name, first_name, credential, role, active, must_change_pw, "
                              "theme, mode FROM users WHERE id = ?", (uid,)).fetchone()
                if self.s.idle_minutes and idle > self.s.idle_minutes * 60:
                    c.execute("UPDATE sessions SET revoked_at = ?, revoked_reason = 'idle' WHERE id = ?", (now(), sid))
                    audit(c, u, "session_timeout", client=self.client(req))
                    out = ("idle", None)
                elif not u or not u["active"]:
                    c.execute("UPDATE sessions SET revoked_at = ?, revoked_reason = 'disabled' WHERE id = ?", (now(), sid))
                    out = ("disabled", None)
                elif touch and idle > 15:   # background polling doesn't count as activity, so idle sign-out still works
                    c.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now(), sid))
        if out:
            req.session.clear()
            if out[0] == "idle":
                req.session["flash"] = [["info", f"Signed out after {self.s.idle_minutes} minutes without activity."]]
            elif out[0] == "gone" and out[1] not in (None, "logout"):
                req.session["flash"] = [["info", "You were signed out. Please sign in again."]]
            raise Redirect("/login" if out[0] == "disabled" else f"/login?next={req.url.path}")
        if u["must_change_pw"] and req.url.path != "/account/password":
            raise Redirect("/account/password")
        if admin and u["role"] != "admin":
            raise HTTPException(403, "Only administrators can open this page.")
        if roles and u["role"] not in roles:
            raise HTTPException(403, f"Your role ({ROLE_LABELS.get(u['role'], u['role'])}) can't do this.")
        return u

    async def form(self, req: Request) -> dict:
        f = await req.form()
        if f.get("csrf") != req.session.get("csrf"):
            raise HTTPException(400, "This form expired. Go back, reload the page and try again.")
        return {k: (v.strip() if isinstance(v, str) else v) for k, v in f.items()}

    def check_csrf_header(self, req: Request) -> None:
        if req.headers.get("x-csrf-token") != req.session.get("csrf"):
            raise HTTPException(400, "Request expired. Reload the page.")

    def render(self, req: Request, name: str, user: dict | None = None, status_code: int = 200, **ctx) -> HTMLResponse:
        if "csrf" not in req.session:
            req.session["csrf"] = new_csrf()
        from . import themes
        b = self.brand.get()
        pref_theme = (user or {}).get("theme") if b["allow_user_theme"] else None
        pref_mode = (user or {}).get("mode") or (None if (user and user.get("id")) else req.cookies.get("drp_mode"))
        theme, mode = themes.resolve(pref_theme, pref_mode, b["theme"], b["mode"])
        ctx.update(
            request=req, user=user, site=self.brand.site_name(), csrf=req.session["csrf"],
            brand=b, brand_title=self.brand.title(), theme=theme, mode=mode,
            theme_ver=themes.css_version(b["accent"]),
            flashes=req.session.pop("flash", []), drug_meta=self.drug_meta, has_drugs=bool(self.drugs),
            sample_data=self.drug_data_state(),
            now_iso=ctx.get("now_iso") or now(), targets=ctx.get("targets"),
            clock=datetime.now().astimezone(), clock_suspect=datetime.now().year < 2025,
            encrypted=self.ehr.encrypted, public_reference=self.s.public_reference,
        )
        return self.tpl.TemplateResponse(req, name, ctx, status_code=status_code)

    # ---------------- chart access
    def access(self, req: Request, user: dict, p: dict) -> str | None:
        """'full' (triage, or assigned provider), 'justified' (reason given recently), or None."""
        if user["role"] == "triage":
            return "full"
        if user["role"] == "provider" and p["assigned_to"] == user["id"]:
            return "full"
        grants = req.session.get("grants", {})
        exp = grants.get(str(p["id"]))
        if exp and exp > time.time():
            return "justified"
        if exp:
            grants.pop(str(p["id"]), None)
            req.session["grants"] = grants
        return None

    def require_chart(self, req: Request, user: dict, p: dict, write: bool = False) -> str:
        a = self.access(req, user, p)
        if a is None:
            raise Redirect(f"/patients/{p['id']}/access?next={req.url.path}")
        if write and user["role"] not in CLINICAL_WRITE:
            raise HTTPException(403, "Viewers can read charts but not change them.")
        return a

    def grant(self, req: Request, pid: int) -> None:
        g = dict(req.session.get("grants", {}))
        g[str(pid)] = int(time.time() + self.s.grant_minutes * 60)
        req.session["grants"] = g

    def can_assign(self, user: dict) -> bool:
        return user["role"] in ("triage", "admin")

    def can_handoff(self, user: dict, p: dict) -> bool:
        return self.can_assign(user) or (user["role"] == "provider" and p["assigned_to"] == user["id"])

    # ---------------- data helpers
    def assignees(self, c) -> list[dict]:
        return c.execute(f"SELECT id, full_name, role FROM users WHERE active = 1 AND role IN ({','.join('?' * len(ASSIGNABLE))}) "
                         "ORDER BY last_name, first_name", ASSIGNABLE).fetchall()

    def get_patient(self, c, pid: int) -> dict:
        p = c.execute("""SELECT p.*, a.full_name AS assigned_name, s.full_name AS seen_name,
                                l.label AS location_name, l.kind AS location_kind
                         FROM patients p LEFT JOIN users a ON a.id = p.assigned_to LEFT JOIN users s ON s.id = p.last_seen_by
                         LEFT JOIN loc_v l ON l.id = p.location_id
                         WHERE p.id = ?""", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "Patient not found")
        return p

    def get_encounter(self, c, eid: int) -> dict:
        e = c.execute("SELECT * FROM encounters WHERE id = ?", (eid,)).fetchone()
        if not e:
            raise HTTPException(404, "Encounter not found")
        return e

    def mark_seen(self, c, pid: int, user: dict) -> None:
        t = now()
        c.execute("UPDATE patients SET last_seen_by = ?, last_seen_at = ?, updated_at = ? WHERE id = ?", (user["id"], t, t, pid))

    def patient_rows(self, c, where: str = "", args: tuple = (), order: str = "COALESCE(p.updated_at, p.created_at) DESC",
                     limit: int = 300) -> list[dict]:
        return c.execute(f"""
            SELECT p.*, a.full_name AS assigned_name, s.full_name AS seen_name,
                   l.label AS location_name, l.kind AS location_kind,
                   (SELECT e.id FROM encounters e WHERE e.patient_id = p.id AND e.status = 'open' ORDER BY e.started_at DESC LIMIT 1) AS open_eid,
                   (SELECT e.chief_complaint FROM encounters e WHERE e.patient_id = p.id ORDER BY e.started_at DESC LIMIT 1) AS complaint,
                   (SELECT e.history FROM encounters e WHERE e.patient_id = p.id ORDER BY e.started_at LIMIT 1) AS first_report,
                   (SELECT e.location FROM encounters e WHERE e.patient_id = p.id ORDER BY e.started_at LIMIT 1) AS first_location
            FROM patients p LEFT JOIN users a ON a.id = p.assigned_to LEFT JOIN users s ON s.id = p.last_seen_by
            LEFT JOIN loc_v l ON l.id = p.location_id
            {('WHERE ' + where) if where else ''} ORDER BY {order} LIMIT {int(limit)}""", args).fetchall()
