"""Time zone for the whole unit (web console, audit trail, backups and touchscreen clock).

Set under Admin > Date & time. The choice is stored in the EHR settings and applied to this
process with TZ + tzset, so every local timestamp written afterwards uses it. Times already
stored carry their UTC offset and are converted for display, so older entries show correctly too.
Default: DRP_TZ, else TZ, else the system zone (UTC in Docker).
"""
from __future__ import annotations

import os
import time
import zoneinfo
from datetime import datetime

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

COMMON = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles",
    "America/Anchorage", "Pacific/Honolulu", "America/Puerto_Rico", "Pacific/Guam", "UTC",
    "America/Halifax", "America/Mexico_City", "Europe/London", "Europe/Berlin", "Australia/Sydney",
]


def valid(name: str | None) -> bool:
    if not name:
        return False
    try:
        zoneinfo.ZoneInfo(name)
        return name in zoneinfo.available_timezones() or name == "UTC"
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False


def all_zones() -> list[str]:
    return sorted(z for z in zoneinfo.available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/")))


def current() -> str:
    return os.environ.get("TZ") or time.tzname[0]


def apply(name: str | None) -> bool:
    """Switch this process to the zone. Returns False (and changes nothing) for an unknown zone."""
    if not valid(name):
        return False
    os.environ["TZ"] = name
    if hasattr(time, "tzset"):
        time.tzset()
    return True


def startup(c) -> str:
    """Apply the saved zone (or the environment default) when the server starts."""
    from .ehr import get_setting
    name = get_setting(c, "timezone") or os.environ.get("DRP_TZ") or os.environ.get("TZ")
    if name and apply(name):
        return name
    return current()


def label(name: str) -> str:
    try:
        now = datetime.now(zoneinfo.ZoneInfo(name))
    except Exception:
        return name
    off = now.strftime("%z")
    return f"{name.replace('_', ' ')} (UTC{off[:3]}:{off[3:]}, now {now:%H:%M})"


def to_local(v) -> datetime | None:
    """Stored ISO timestamp -> aware datetime in the unit's zone (naive values are taken as already local)."""
    if not v:
        return None
    if isinstance(v, datetime):
        d = v
    else:
        try:
            d = datetime.fromisoformat(str(v))
        except ValueError:
            return None
    return d.astimezone() if d.tzinfo else d


def parse_local(v: str | None) -> datetime | None:
    """A value from a datetime-local form field, read as the unit's local time."""
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()   # astimezone on a naive value assumes local (now = the unit's zone)


def register(app, ctx) -> None:
    from .auditlog import audit
    from .ehr import set_setting

    def _start():
        with ctx.ehr.session() as c:
            startup(c)
        _publish(ctx)
    ctx.ehr.on_ready(_start)

    @app.get("/admin/time", response_class=HTMLResponse)
    async def time_view(req: Request):
        user = ctx.current_user(req, admin=True)
        cur = current()
        zones = all_zones()
        return ctx.render(req, "time.html", user, tz=cur, tz_label=label(cur), common=[(z, label(z)) for z in COMMON if valid(z)],
                          zones=zones, server_now=datetime.now().astimezone())

    @app.post("/admin/time")
    async def time_save(req: Request):
        user = ctx.current_user(req, admin=True)
        f = await ctx.form(req)
        name = (f.get("timezone") or "").strip()
        old = current()
        if not apply(name):
            ctx.flash(req, f"'{name[:60]}' isn't a known time zone. Pick one from the list, e.g. America/Chicago.", "error")
            return RedirectResponse("/admin/time", 303)
        with ctx.ehr.session() as c:
            set_setting(c, "timezone", name)
            audit(c, user, "timezone_settings", detail=f"{old} -> {name}", client=ctx.client(req))
        _publish(ctx)
        ctx.flash(req, f"Time zone set to {label(name)}. The touchscreen picks it up within a few seconds.")
        return RedirectResponse("/admin/time", 303)


def _publish(ctx) -> None:
    from .branding import publish_to_kiosk
    publish_to_kiosk(ctx.s, ctx.brand.get())
