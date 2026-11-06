"""Status board for a wall screen (the Pi's second HDMI output, or any TV on the unit's Wi-Fi).

Read-only and refreshed every few seconds. By default it is sanitized: room status and counts only,
nothing that identifies a patient. An admin can turn on names ("M. Hayes") for screens inside the
facility. Who can open it:
  * the Pi itself (requests from 127.0.0.1 / ::1), so the local HDMI screen needs no login;
  * a screen holding a display key (Admin > Status board), kept in a cookie after the first visit;
  * anyone signed in to the console.
A display key opens this board and nothing else.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import flow
from .auditlog import audit
from .ehr import allergy_status, get_setting, now, set_setting
from .history import LIMITED_CODE
from .web import Redirect

COOKIE = "drp_display"
DEFAULTS = {"names": False, "show_morgue": False, "mode": "dark", "allow_local": True}
REFRESH_SECONDS = 10


def settings_of(c) -> dict:
    v = dict(DEFAULTS)
    v.update(get_setting(c, "board", {}) or {})
    return v


def short_name(first: str | None, last: str | None) -> str:
    """'M. Hayes' — first initial and last name, as shown on a named board."""
    first, last = (first or "").strip(), (last or "").strip()
    if last and first:
        return f"{first[0].upper()}. {last}"
    return last or (f"{first[0].upper()}." if first else "Unidentified")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def build(c, cfg: dict) -> dict:
    """Everything the board shows. With names off, nothing here identifies a patient."""
    t = now()
    tg = flow.targets(c)
    locs = flow.all_locations(c)
    pts = c.execute("""SELECT p.id, p.first_name, p.last_name, p.location_id, p.location_at, p.status, p.allergies, p.code_status,
                              u.first_name AS u_first, u.last_name AS u_last, u.credential AS u_cred
                       FROM patients p LEFT JOIN users u ON u.id = p.assigned_to
                       WHERE p.incoming = 0 AND p.location_id IS NOT NULL AND p.status IN ('active', 'deceased')
                       ORDER BY p.location_at""").fetchall()
    by_loc: dict[int, list] = {}
    for p in pts:
        by_loc.setdefault(p["location_id"], []).append(p)

    def people(here):
        return [{
            "name": short_name(p["first_name"], p["last_name"]),
            "provider": (short_name(p["u_first"], p["u_last"]) + (f" {p['u_cred']}" if p["u_cred"] else "")) if p["u_last"] else "",
            "allergy": allergy_status(p["allergies"]), "dnr": p["code_status"] in LIMITED_CODE, "deceased": p["status"] == "deceased",
        } for p in here] if cfg["names"] else []

    def spot(l):
        here = by_loc.get(l["id"], [])
        if here:
            state, since = "occupied", min(p["location_at"] or t for p in here)
        elif flow.cleanable(l):
            state, since = l["state"], (l["state_at"] if l["state"] != "ready" else None)
        else:
            state, since = "open", None
        return {"name": l["own_name"], "kind": l["kind"], "state": state, "since": since, "n": len(here),
                "capacity": l["capacity"], "over": bool(l["capacity"] and len(here) > l["capacity"]), "people": people(here)}

    tiles = {"room": [], "or": [], "other": [], "morgue": [], "triage": []}
    counts = {"triage": 0, "room": 0, "or": 0, "other": 0, "morgue": 0}
    spots = []                       # every bed / room a patient can be placed in, for the ready/cleaning summary
    tops: dict[int, dict] = {}
    for l in locs:
        counts[l["kind"]] += len(by_loc.get(l["id"], []))
        if l["parent_id"]:
            parent = tops.get(l["parent_id"])
            if parent is not None:
                s = spot(l)
                parent["beds"].append(s)
                spots.append(s)
            continue
        if l["n_children"]:
            tile = {"name": l["own_name"], "kind": l["kind"], "beds": [], "state": "group"}
        else:
            tile = spot(l)
            tile["beds"] = []
            spots.append(tile)
        tops[l["id"]] = tile
        tiles[l["kind"]].append(tile)
    for tile in tops.values():
        if tile["state"] == "group":
            b = tile["beds"]
            tile["n"] = sum(x["n"] for x in b)
            tile["capacity"] = len(b)
            tile["occupied"] = sum(1 for x in b if x["state"] == "occupied")
            tile["ready"] = sum(1 for x in b if x["state"] in ("ready", "open"))
    tiles["triage"] = [x for x in tiles["triage"] if x["beds"]]          # a plain triage area is shown as a count only
    if not cfg["show_morgue"]:
        tiles["morgue"] = []
    queue = c.execute("SELECT queued_at FROM patients WHERE status = 'active' AND incoming = 0 AND assigned_to IS NULL "
                      "AND queued_at IS NOT NULL ORDER BY queued_at").fetchall()
    incoming = c.execute("SELECT eta_at FROM patients WHERE status = 'active' AND incoming = 1 ORDER BY eta_at IS NULL, eta_at").fetchall()
    active = c.execute("SELECT COUNT(*) AS n FROM patients WHERE status = 'active' AND incoming = 0").fetchone()["n"]
    rooms = [x for x in spots if x["kind"] in ("room", "or") and x["state"] != "open"]
    summary = {
        "active": active, "waiting": len(queue), "oldest_wait": queue[0]["queued_at"] if queue else None,
        "incoming": len(incoming), "next_eta": incoming[0]["eta_at"] if incoming else None,
        "in_triage": counts["triage"], "in_rooms": counts["room"], "in_or": counts["or"], "in_other": counts["other"],
        "morgue": counts["morgue"],
        "ready": sum(1 for x in rooms if x["state"] == "ready"),
        "dirty": sum(1 for x in rooms if x["state"] in ("dirty", "cleaning")),
    }
    return {"tiles": tiles, "summary": summary, "targets": tg, "now_iso": t}


def register(app, ctx) -> None:
    ehr, flash, form = ctx.ehr, ctx.flash, ctx.form

    def who(req: Request, c) -> str | None:
        """'local', 'user' or 'key:<name>'; None when this screen isn't allowed to see the board."""
        cfg = settings_of(c)
        host = req.client.host if req.client else ""
        if cfg["allow_local"] and host in ("127.0.0.1", "::1"):
            return "local"
        raw = req.cookies.get(COOKIE)
        if raw:
            k = c.execute("SELECT * FROM display_keys WHERE key_hash = ? AND revoked_at IS NULL", (_hash(raw),)).fetchone()
            if k:
                last = k["last_seen_at"]
                if not last or (datetime.now().astimezone() - datetime.fromisoformat(last)).total_seconds() > 300:
                    c.execute("UPDATE display_keys SET last_seen_at = ?, last_client = ? WHERE id = ?", (now(), host, k["id"]))
                return f"key:{k['name']}"
        if req.session.get("uid"):
            try:
                ctx.current_user(req, touch=False)
                return "user"
            except (Redirect, HTTPException):
                return None
        return None

    def page(req, name, status_code=200, **kw):
        with ehr.session() as c:
            cfg = settings_of(c)
        b = ctx.brand.get()
        from . import themes
        theme, _mode = themes.resolve(None, None, b["theme"], b["mode"])
        return ctx.tpl.TemplateResponse(req, name, dict(
            request=req, brand=b, brand_title=ctx.brand.title(), theme=theme, mode=cfg["mode"], cfg=cfg,
            theme_ver=themes.css_version(b["accent"]), refresh=REFRESH_SECONDS,
            offset_min=int(datetime.now().astimezone().utcoffset().total_seconds() // 60), **kw), status_code=status_code)

    @app.get("/board", response_class=HTMLResponse)
    async def board(req: Request, key: str = ""):
        if key:
            with ehr.session() as c:
                k = c.execute("SELECT * FROM display_keys WHERE key_hash = ? AND revoked_at IS NULL", (_hash(key),)).fetchone()
                if k:
                    audit(c, None, "display_connect", detail=f"'{k['name']}' from {ctx.client(req)}", client=ctx.client(req))
            if not k:
                return page(req, "board_locked.html", status_code=403, bad_key=True)
            r = RedirectResponse("/board", 303)      # drop the key from the address bar
            r.set_cookie(COOKIE, key, max_age=400 * 86400, httponly=True, samesite="strict", secure=ctx.s.secure_cookies)
            return r
        with ehr.session() as c:
            w = who(req, c)
            data = build(c, settings_of(c)) if w else None
        if not w:
            return page(req, "board_locked.html", status_code=403, bad_key=False)
        return page(req, "board.html", **data)

    @app.get("/board/data", response_class=HTMLResponse)
    async def board_data(req: Request):
        with ehr.session() as c:
            w = who(req, c)
            data = build(c, settings_of(c)) if w else None
        if not w:
            raise HTTPException(403, "This screen isn't allowed to show the status board.")
        return page(req, "_board_body.html", **data)

    # ---------------- admin
    @app.get("/admin/board", response_class=HTMLResponse)
    async def board_admin(req: Request):
        user = ctx.current_user(req, admin=True)
        with ehr.session() as c:
            cfg = settings_of(c)
            keys = c.execute("""SELECT k.*, u.full_name AS by_name FROM display_keys k LEFT JOIN users u ON u.id = k.created_by
                                ORDER BY k.revoked_at IS NOT NULL, k.id DESC""").fetchall()
        new_key = req.session.pop("new_display_key", None)
        return ctx.render(req, "board_admin.html", user, cfg=cfg, keys=keys, new_key=new_key,
                          base_url=f"{req.url.scheme}://{req.url.netloc}")

    @app.post("/admin/board")
    async def board_settings(req: Request):
        user = ctx.current_user(req, admin=True)
        f = await form(req)
        new = {"names": f.get("names") == "1", "show_morgue": f.get("show_morgue") == "1",
               "mode": f.get("mode") if f.get("mode") in ("dark", "light") else "dark", "allow_local": f.get("allow_local") == "1"}
        with ehr.session() as c:
            old = settings_of(c)
            set_setting(c, "board", new)
            labels = {"names": "patient names", "show_morgue": "morgue tile", "allow_local": "open on the unit's own screen",
                      "mode": "look"}
            changes = [f"{labels[k]} {'on' if new[k] else 'off'}" if isinstance(new[k], bool) else f"{labels[k]} {new[k]}"
                       for k in new if new[k] != old.get(k)]
            audit(c, user, "board_settings", detail=", ".join(changes) or "no changes", client=ctx.client(req))
        if new["names"] and not old["names"]:
            flash(req, "Patient names (first initial and last name) are now shown on every status board screen.", "error")
        else:
            flash(req, "Status board settings saved.")
        return RedirectResponse("/admin/board", 303)

    @app.post("/admin/board/keys")
    async def key_create(req: Request):
        user = ctx.current_user(req, admin=True)
        f = await form(req)
        name = (f.get("name") or "").strip()[:60]
        if not name:
            flash(req, "Name the screen, e.g. 'Hallway TV'.", "error")
            return RedirectResponse("/admin/board", 303)
        key = secrets.token_urlsafe(24)
        with ehr.session() as c:
            c.execute("INSERT INTO display_keys(name, key_hash, created_at, created_by) VALUES (?,?,?,?)",
                      (name, _hash(key), now(), user["id"]))
            audit(c, user, "display_key_create", detail=f"'{name}'", client=ctx.client(req))
        req.session["new_display_key"] = {"name": name, "key": key}
        return RedirectResponse("/admin/board", 303)

    @app.post("/admin/board/keys/{kid}/revoke")
    async def key_revoke(req: Request, kid: int):
        user = ctx.current_user(req, admin=True)
        await form(req)
        with ehr.session() as c:
            k = c.execute("SELECT * FROM display_keys WHERE id = ?", (kid,)).fetchone()
            if not k:
                raise HTTPException(404, "Key not found")
            c.execute("UPDATE display_keys SET revoked_at = ?, revoked_by = ? WHERE id = ? AND revoked_at IS NULL", (now(), user["id"], kid))
            audit(c, user, "display_key_revoke", detail=f"'{k['name']}'", client=ctx.client(req))
        flash(req, f"Key for '{k['name']}' revoked. That screen stops updating within {REFRESH_SECONDS} seconds.")
        return RedirectResponse("/admin/board", 303)
