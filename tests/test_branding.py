"""Branding (title, tagline, icon, accent) and themes (6 themes x light/dark/auto)."""
import base64
import json
import re

from conftest import audit_messages

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def test_every_theme_passes_contrast_checks():
    from drprepper.server.themes import THEMES, contrast_report
    assert len(THEMES) >= 6
    assert contrast_report() == []


def test_accent_override_stays_readable():
    from drprepper.server.themes import accent_variants, contrast
    for colour in ("#ffcc00", "#00ff00", "#0000ff", "#222222", "#ff00ff"):
        v = accent_variants(colour)
        assert contrast(v["light"]["accent"], "#ffffff") >= 4.5
        assert contrast(v["dark"]["accent"], "#1b1c23") >= 4.5
        assert contrast(v["light"]["accent-ink"], v["light"]["accent"]) >= 4.5


def test_theme_css_and_default_attributes(new_browser):
    b = new_browser()
    css = b.get("/theme.css").text
    for key in ("field", "clinic", "desert", "nightops", "contrast", "slate"):
        assert f'[data-theme="{key}"][data-mode="dark"]' in css
    page = b.get("/login").text
    assert 'data-theme="field" data-mode="auto"' in page and "/theme.css?v=" in page


def test_quick_mode_toggle_signed_in_and_anonymous(app, new_browser):
    a = new_browser()
    a.get("/login")
    r = a.post("/display/mode", {"mode_now": "auto", "next": "/drugs"})
    assert r.status_code == 303 and r.headers["location"] == "/drugs"
    assert 'data-mode="light"' in a.get("/login").text
    s = new_browser("smith")
    s.post("/display/mode", {"mode_now": "auto", "next": "/"})
    s.get("/")
    s.post("/display/mode", {"mode_now": "light", "next": "/"})
    assert 'data-mode="dark"' in s.get("/").text
    with app.state.ehr.session() as c:
        assert c.execute("SELECT mode FROM users WHERE username='smith'").fetchone()["mode"] == "dark"
    # bad redirect target is ignored
    s.get("/")
    assert s.post("/display/mode", {"mode_now": "dark", "next": "//evil.example"}).headers["location"] == "/"


def test_user_theme_choice_and_admin_lock(app, new_browser):
    s = new_browser("smith")
    s.get("/account/display")
    s.post("/account/display", {"theme": "nightops", "mode": "dark"})
    assert 'data-theme="nightops" data-mode="dark"' in s.get("/").text
    a = new_browser("admin")
    a.get("/admin/branding")
    a.post("/admin/branding", {"theme": "clinic", "mode": "light", "show": "both"})   # allow_user_theme unticked
    page = s.get("/").text
    assert 'data-theme="clinic"' in page and 'data-mode="dark"' in page             # theme locked, own mode kept
    assert "set one theme (Clinic)" in s.get("/account/display").text


def test_branding_title_icon_and_kiosk_copy(app, new_browser, settings):
    a = new_browser("admin")
    a.get("/admin/branding")
    r = a.c.post("/admin/branding", data={"csrf": a.csrf, "title": "County MCI Field Hospital", "tagline": "Station 4",
                                          "show": "both", "theme": "slate", "mode": "auto", "allow_user_theme": "1"},
                 files={"icon": ("logo.png", PNG, "image/png")})
    assert r.status_code == 303
    page = new_browser().get("/login").text
    assert "<title>Log in · County MCI Field Hospital</title>" in page and 'class="tagline">Station 4' in page
    assert re.search(r'<img src="/branding/icon\?v=[^"]+" alt="">', page)
    icon = new_browser().get("/branding/icon")
    assert icon.headers["content-type"] == "image/png" and icon.content == PNG
    brand = json.loads((settings.kiosk_dir / "branding.json").read_text())
    assert brand["title"] == "County MCI Field Hospital" and (settings.kiosk_dir / brand["icon"]).read_bytes() == PNG
    # icon only
    a.get("/admin/branding")
    a.post("/admin/branding", {"title": "County MCI Field Hospital", "show": "icon", "theme": "slate", "mode": "auto"})
    page = new_browser().get("/login").text
    assert 'class="bt"' not in page.split("</header>")[0]
    # remove icon -> title comes back
    a.get("/admin/branding")
    a.post("/admin/branding", {"title": "County MCI Field Hospital", "show": "icon", "icon_remove": "1", "theme": "slate", "mode": "auto"})
    page = new_browser().get("/login").text
    assert "County MCI Field Hospital" in page.split("</header>")[0] and "/branding/icon" not in page
    assert new_browser().get("/branding/icon").status_code == 404
    assert any("changed branding and theme settings" in m and "new icon (PNG" in m for m in audit_messages(app))


def test_icon_rejects_svg_and_oversize(new_browser):
    a = new_browser("admin")
    a.get("/admin/branding")
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    a.c.post("/admin/branding", data={"csrf": a.csrf, "theme": "field", "mode": "auto", "show": "both"},
             files={"icon": ("x.svg", svg, "image/svg+xml")})
    assert "SVG isn&#39;t accepted" in a.get("/admin/branding").text
    a.c.post("/admin/branding", data={"csrf": a.csrf, "theme": "field", "mode": "auto", "show": "both"},
             files={"icon": ("big.png", PNG + b"\0" * 600_000, "image/png")})
    assert "512 KB or smaller" in a.get("/admin/branding").text
    assert new_browser().get("/branding/icon").status_code == 404


def test_accent_colour(new_browser):
    a = new_browser("admin")
    a.get("/admin/branding")
    a.post("/admin/branding", {"accent": "#FFCC00", "accent_on": "1", "theme": "field", "mode": "auto", "show": "both"})
    page = new_browser().get("/login").text
    assert "data-brand-accent" in page
    v = re.search(r'/theme.css\?v=(\w+)', page).group(1)
    css = new_browser().get(f"/theme.css?v={v}").text
    assert "[data-brand-accent] { --accent: #866b00" in css
    a.get("/admin/branding")
    a.post("/admin/branding", {"accent": "#ffcc00", "theme": "field", "mode": "auto", "show": "both"})  # box unticked
    assert "data-brand-accent" not in new_browser().get("/login").text


def test_every_page_renders_in_every_theme(app, new_browser):
    from drprepper.server.themes import THEMES
    a = new_browser("admin")
    for key in THEMES:
        for mode in ("light", "dark"):
            a.get("/account/display")
            a.post("/account/display", {"theme": key, "mode": mode})
            for path in ("/", "/patients", "/drugs?q=narcan", "/tox/opioids", "/admin/audit", "/admin/branding"):
                r = a.get(path)
                assert r.status_code == 200 and f'data-theme="{key}" data-mode="{mode}"' in r.text, (key, mode, path)


def test_builtin_icons_selectable_and_switch_back_to_upload(app, new_browser, settings):
    from drprepper.server.branding import BUILTIN_ICONS, STATIC_ICONS
    assert len(BUILTIN_ICONS) >= 6
    for key in BUILTIN_ICONS:
        data = (STATIC_ICONS / f"{key}.png").read_bytes()
        assert data.startswith(b"\x89PNG")
    a = new_browser("admin")
    page = a.get("/admin/branding").text
    assert page.count('name="icon_choice"') == len(BUILTIN_ICONS) + 2
    assert new_browser().get("/static/icons/heartbeat.png").status_code == 200
    base = {"title": "Station 4", "show": "both", "theme": "field", "mode": "auto"}
    a.post("/admin/branding", {**base, "icon_choice": "builtin:heartbeat"})
    icon = new_browser().get("/branding/icon")
    assert icon.headers["content-type"] == "image/png"
    assert icon.content == (STATIC_ICONS / "heartbeat.png").read_bytes()
    assert '<link rel="icon" href="/branding/icon' in new_browser().get("/login").text
    brand = json.loads((settings.kiosk_dir / "branding.json").read_text())
    assert (settings.kiosk_dir / brand["icon"]).read_bytes() == icon.content
    assert 'value="builtin:heartbeat" checked' in a.get("/admin/branding").text
    # unknown key is ignored
    a.post("/admin/branding", {**base, "icon_choice": "builtin:../../etc/passwd"})
    assert new_browser().get("/branding/icon").content == icon.content
    # upload own -> selected; pick a builtin; switch back to the upload without re-uploading
    a.get("/admin/branding")
    a.c.post("/admin/branding", data={"csrf": a.csrf, **base, "icon_choice": "builtin:tent"},
             files={"icon": ("logo.png", PNG, "image/png")})
    assert new_browser().get("/branding/icon").content == PNG
    a.get("/admin/branding")
    a.post("/admin/branding", {**base, "icon_choice": "builtin:shield"})
    assert new_browser().get("/branding/icon").content == (STATIC_ICONS / "shield.png").read_bytes()
    assert a.get("/branding/custom-icon").content == PNG
    assert new_browser().get("/branding/custom-icon").status_code in (303, 401, 403)
    a.get("/admin/branding")
    a.post("/admin/branding", {**base, "icon_choice": "custom"})
    assert new_browser().get("/branding/icon").content == PNG
    a.get("/admin/branding")
    a.post("/admin/branding", {**base, "icon_choice": "none"})
    assert new_browser().get("/branding/icon").status_code == 404
    assert any("builtin:shield" in m for m in audit_messages(app))
