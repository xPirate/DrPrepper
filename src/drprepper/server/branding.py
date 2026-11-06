"""Branding (title, tagline, icon, accent colour) and display preferences (theme, light/dark)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import themes
from .auditlog import audit
from .ehr import get_setting, now, set_setting

MAX_ICON = 512 * 1024
ICON_TYPES = {  # magic bytes -> (extension, media type). SVG is refused: it can carry scripts.
    b"\x89PNG\r\n\x1a\n": ("png", "image/png"),
    b"\xff\xd8\xff": ("jpg", "image/jpeg"),
}
# Built-in icons: original artwork in static/icons (see tools/make_icons.py). None of them uses the
# red cross / red crescent emblems or the Star of Life, which are protected marks.
BUILTIN_ICONS = {"cross": "Medical cross", "heartbeat": "Heartbeat", "capsule": "Capsule", "stethoscope": "Stethoscope",
                 "kit": "First-aid kit", "asclepius": "Rod of Asclepius", "tent": "Field tent", "shield": "Shield"}
STATIC_ICONS = Path(__file__).parent / "static" / "icons"
DEFAULTS = {"title": "", "tagline": "", "show": "both", "icon": "", "icon_ver": "", "accent": "", "custom_icon": "",
            "theme": themes.DEFAULT_THEME, "mode": "auto", "allow_user_theme": True}
SHOW = {"both": "Icon and title", "title": "Title only", "icon": "Icon only"}


def sniff(data: bytes) -> tuple[str, str] | None:
    for magic, kind in ICON_TYPES.items():
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp", "image/webp")
    return None


def branding_dir(s) -> Path:
    return s.state_dir / "branding"


def icon_path(s, icon: str) -> Path | None:
    """'builtin:<key>' -> packaged PNG; anything else -> the uploaded file."""
    if not icon:
        return None
    if icon.startswith("builtin:"):
        key = icon.split(":", 1)[1]
        return STATIC_ICONS / f"{key}.png" if key in BUILTIN_ICONS else None
    return branding_dir(s) / icon


class Brand:
    """Cached branding settings; reloaded after a save."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._cfg = None

    def get(self) -> dict:
        if self.ctx.ehr.locked:
            return dict(DEFAULTS)          # settings live in the encrypted EHR; defaults until it's unlocked
        if self._cfg is None:
            with self.ctx.ehr.session() as c:
                cfg = dict(DEFAULTS)
                cfg.update(get_setting(c, "branding", {}) or {})
            self._cfg = cfg
        return self._cfg

    def invalidate(self):
        self._cfg = None

    def title(self) -> str:
        return self.get()["title"] or "Dr.Prepper"

    def site_name(self) -> str:
        """Used in page titles and the sign-in page."""
        return self.get()["title"] or self.ctx.s.site_name


def publish_to_kiosk(s, cfg: dict) -> None:
    """The touchscreen shows the same title and icon. The kiosk folder holds no patient data."""
    kd = s.kiosk_dir
    try:
        kd.mkdir(parents=True, exist_ok=True)
        icon_name = ""
        for old in kd.glob("brand-icon.*"):
            old.unlink(missing_ok=True)
        src = icon_path(s, cfg.get("icon", ""))
        ext = src.suffix.lstrip(".") if src else ""
        if src and ext in ("png", "jpg"):           # the touchscreen can't show WebP
            if src.exists():
                icon_name = f"brand-icon.{ext}"
                (kd / icon_name).write_bytes(src.read_bytes())
                (kd / icon_name).chmod(0o644)
        data = {"title": cfg.get("title", ""), "tagline": cfg.get("tagline", ""), "show": cfg.get("show", "both"),
                "icon": icon_name, "timezone": os.environ.get("TZ", "")}
        tmp = kd / "branding.json.tmp"
        tmp.write_text(json.dumps(data))
        tmp.chmod(0o644)
        tmp.replace(kd / "branding.json")
    except OSError:
        pass


def register(app, ctx) -> None:
    s, ehr, render, flash, form = ctx.s, ctx.ehr, ctx.render, ctx.flash, ctx.form
    brand = ctx.brand
    ctx.ehr.on_ready(lambda: publish_to_kiosk(s, brand.get()))

    @app.get("/theme.css")
    async def theme_css():
        return Response(themes.css(brand.get()["accent"]), media_type="text/css",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/branding/icon")
    async def brand_icon():
        p = icon_path(s, brand.get()["icon"])
        if not p or not p.exists():
            raise HTTPException(404, "No icon set")
        kind = sniff(p.read_bytes()[:16])
        return Response(p.read_bytes(), media_type=kind[1] if kind else "application/octet-stream",
                        headers={"Cache-Control": "public, max-age=31536000, immutable",
                                 "Content-Security-Policy": "default-src 'none'"})

    @app.get("/branding/custom-icon")
    async def custom_icon(req: Request):
        ctx.current_user(req, admin=True)
        name = brand.get().get("custom_icon") or ""
        p = branding_dir(s) / name if name else None
        if not p or not p.exists():
            raise HTTPException(404, "No uploaded icon")
        kind = sniff(p.read_bytes()[:16])
        return Response(p.read_bytes(), media_type=kind[1] if kind else "application/octet-stream",
                        headers={"Content-Security-Policy": "default-src 'none'"})

    # ------------------------------------------------------------------ admin branding
    @app.get("/admin/branding", response_class=HTMLResponse)
    async def branding_view(req: Request):
        user = ctx.current_user(req, admin=True)
        return render(req, "branding.html", user, b=brand.get(), themes=themes.THEMES, show_opts=SHOW, builtin=BUILTIN_ICONS,
                      modes=themes.MODES)

    @app.post("/admin/branding")
    async def branding_save(req: Request):
        user = ctx.current_user(req, admin=True)
        raw = await req.form()
        if raw.get("csrf") != req.session.get("csrf"):
            raise HTTPException(400, "This form expired. Reload the page and try again.")
        cfg = dict(brand.get())
        changes = []
        title = (raw.get("title") or "").strip()[:60]
        tagline = (raw.get("tagline") or "").strip()[:80]
        show = raw.get("show") if raw.get("show") in SHOW else "both"
        accent = (raw.get("accent") or "").strip().lower() if raw.get("accent_on") == "1" else ""
        if accent and not themes.HEX.match(accent):
            flash(req, "The accent colour must look like #1f6f5c.", "error")
            return RedirectResponse("/admin/branding", 303)
        theme = raw.get("theme") if raw.get("theme") in themes.THEMES else cfg["theme"]
        mode = raw.get("mode") if raw.get("mode") in themes.MODES else cfg["mode"]
        allow = raw.get("allow_user_theme") == "1"
        choice = raw.get("icon_choice") or ""
        up = raw.get("icon")
        if up is not None and hasattr(up, "read") and getattr(up, "filename", ""):
            data = await up.read()
            if len(data) > MAX_ICON:
                flash(req, "The icon must be 512 KB or smaller.", "error")
                return RedirectResponse("/admin/branding", 303)
            kind = sniff(data)
            if not kind:
                flash(req, "Use a PNG, JPEG or WebP image for the icon. SVG isn't accepted because it can contain scripts.", "error")
                return RedirectResponse("/admin/branding", 303)
            d = branding_dir(s)
            d.mkdir(parents=True, exist_ok=True)
            for old in d.glob("icon.*"):
                old.unlink(missing_ok=True)
            (d / f"icon.{kind[0]}").write_bytes(data)
            cfg["custom_icon"] = f"icon.{kind[0]}"
            choice = "custom"
            changes.append(f"new icon ({kind[0].upper()}, {len(data) // 1024} KB)")
        if choice == "custom" and not cfg.get("custom_icon"):
            flash(req, "Choose a file to upload your own icon.", "error")
            return RedirectResponse("/admin/branding", 303)
        if raw.get("icon_remove") == "1":           # older form field: same as "No icon"
            choice = "none"
        new_icon = cfg["icon"]
        if choice == "none":
            new_icon = ""
        elif choice == "custom":
            new_icon = cfg["custom_icon"]
        elif choice.startswith("builtin:") and choice.split(":", 1)[1] in BUILTIN_ICONS:
            new_icon = choice
        if new_icon != cfg["icon"] or choice == "custom":
            if new_icon != cfg["icon"]:
                changes.append(f"icon: '{cfg['icon'] or 'none'}' -> '{new_icon or 'none'}'")
            cfg["icon"], cfg["icon_ver"] = new_icon, (now()[:19].replace(":", "") if new_icon else "")
        if show in ("icon",) and not cfg["icon"]:
            show = "both"
            flash(req, "No icon is set, so the title stays visible.", "warn")
        for k, v in (("title", title), ("tagline", tagline), ("show", show), ("accent", accent), ("theme", theme),
                     ("mode", mode), ("allow_user_theme", allow)):
            if cfg.get(k) != v:
                changes.append(f"{k}: '{cfg.get(k)}' -> '{v}'")
                cfg[k] = v
        with ehr.session() as c:
            set_setting(c, "branding", cfg)
            audit(c, user, "branding_settings", detail="; ".join(changes) or "no changes", client=ctx.client(req))
        brand.invalidate()
        publish_to_kiosk(s, cfg)
        flash(req, "Branding saved.")
        return RedirectResponse("/admin/branding", 303)

    # ------------------------------------------------------------------ personal display
    @app.get("/account/display", response_class=HTMLResponse)
    async def display_view(req: Request):
        user = ctx.current_user(req)
        return render(req, "display.html", user, themes=themes.THEMES, modes=themes.MODES, b=brand.get())

    @app.post("/account/display")
    async def display_save(req: Request):
        user = ctx.current_user(req)
        f = await form(req)
        b = brand.get()
        theme = f.get("theme") if f.get("theme") in themes.THEMES else None
        mode = f.get("mode") if f.get("mode") in themes.MODES else None
        if not b["allow_user_theme"]:
            theme = None
        with ehr.session() as c:
            c.execute("UPDATE users SET theme = ?, mode = ? WHERE id = ?", (theme if f.get("theme") != "" else None, mode, user["id"]))
        flash(req, "Display settings saved.")
        return RedirectResponse("/account/display", 303)

    @app.post("/display/mode")
    async def quick_mode(req: Request):
        """Header button: cycle auto -> light -> dark. Signed-in users keep it; others get a cookie."""
        f = await form(req)
        nxt = f.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        current = f.get("mode_now") if f.get("mode_now") in themes.MODES else "auto"
        new = themes.MODES[(themes.MODES.index(current) + 1) % len(themes.MODES)]
        resp = RedirectResponse(nxt, 303)
        if req.session.get("uid"):
            user = ctx.current_user(req)
            with ehr.session() as c:
                c.execute("UPDATE users SET mode = ? WHERE id = ?", (new, user["id"]))
        else:
            resp.set_cookie("drp_mode", new, max_age=365 * 86400, samesite="strict", secure=s.secure_cookies, httponly=True)
        return resp
