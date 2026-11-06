"""Console themes. Each theme has a light and a dark palette; 'auto' follows the device.

Semantic colours (danger, allergy, warnings, OK) keep their meaning in every theme so an
allergy badge is always red with white text, whatever the look. `contrast_report()` checks
every palette against WCAG AA; the test suite runs it.
"""
from __future__ import annotations

import functools
import hashlib
import re

MODES = ("auto", "light", "dark")
TOKENS = ("bg", "panel", "ink", "muted", "line", "accent", "accent-ink", "danger", "danger-bg", "danger-solid",
          "danger-ink", "warn", "warn-bg", "ok", "ok-bg", "od", "notice", "allergy", "allergy-ink", "unknown",
          "unknown-ink", "nkda", "nkda-ink", "header", "header-ink")

_COMMON_LIGHT = {"danger": "#b3261e", "danger-bg": "#fdecea", "danger-solid": "#b3261e", "danger-ink": "#ffffff",
                 "warn": "#7a4f00", "warn-bg": "#fff4d6", "ok": "#1d6b35", "ok-bg": "#e5f4e9", "od": "#a3290f",
                 "notice": "#2d5a8c", "allergy": "#b3261e", "allergy-ink": "#ffffff", "unknown": "#f0b429",
                 "unknown-ink": "#2b1d00", "nkda": "#55605a", "nkda-ink": "#ffffff"}
_COMMON_DARK = {"danger": "#ff8a80", "danger-bg": "#3a1714", "danger-solid": "#c62828", "danger-ink": "#ffffff",
                "warn": "#f2c35b", "warn-bg": "#3a2e10", "ok": "#7fd49a", "ok-bg": "#163222", "od": "#ff8a65",
                "notice": "#8db7e8", "allergy": "#c62828", "allergy-ink": "#ffffff", "unknown": "#e0a526",
                "unknown-ink": "#231700", "nkda": "#5b6660", "nkda-ink": "#ffffff"}


def _p(base: dict, **kw) -> dict:
    d = dict(base)
    d.update({k.replace("_", "-"): v for k, v in kw.items()})
    d.setdefault("header", d["panel"])
    d.setdefault("header-ink", d["ink"])
    return d


THEMES: dict[str, dict] = {
    "field": {
        "name": "Field", "blurb": "Muted greens. The original look.",
        "light": _p(_COMMON_LIGHT, bg="#f4f5f2", panel="#ffffff", ink="#1d2320", muted="#5f6963", line="#d9ddd7",
                    accent="#1f6f5c", accent_ink="#ffffff"),
        "dark": _p(_COMMON_DARK, bg="#141816", panel="#1d2320", ink="#e8ece9", muted="#9aa49e", line="#333b37",
                   accent="#4fb596", accent_ink="#0d1411"),
    },
    "clinic": {
        "name": "Clinic", "blurb": "Cool blues and white, like a hospital chart.",
        "light": _p(_COMMON_LIGHT, bg="#eef3f8", panel="#ffffff", ink="#13233a", muted="#56657a", line="#d3dde8",
                    accent="#1c5fa8", accent_ink="#ffffff", header="#163a63", header_ink="#ffffff"),
        "dark": _p(_COMMON_DARK, bg="#0d1520", panel="#15202e", ink="#e3ecf6", muted="#97a8bb", line="#2a3a4e",
                   accent="#6aa9ec", accent_ink="#06121f", header="#0f2540", header_ink="#e3ecf6"),
    },
    "desert": {
        "name": "Desert", "blurb": "Sand and khaki with a rust accent.",
        "light": _p(_COMMON_LIGHT, bg="#f3eee3", panel="#fffcf5", ink="#2b2418", muted="#6b604c", line="#e0d6c2",
                    accent="#9a4a1c", accent_ink="#ffffff", header="#5c4a2e", header_ink="#fff8ea"),
        "dark": _p(_COMMON_DARK, bg="#1b1712", panel="#25201a", ink="#efe6d6", muted="#aa9d86", line="#3d342a",
                   accent="#e0915c", accent_ink="#1b0f06", header="#2e271e", header_ink="#efe6d6"),
    },
    "nightops": {
        "name": "Night ops", "blurb": "Dark mode is red on black to protect night vision; light mode is warm and dim.",
        "light": _p(_COMMON_LIGHT, bg="#efe9e4", panel="#f8f3ee", ink="#2a1b17", muted="#6b5650", line="#ddd0c8",
                    accent="#8c2f1f", accent_ink="#ffffff", header="#3a1f1a", header_ink="#f8e9e4"),
        "dark": _p(_COMMON_DARK, bg="#050202", panel="#0f0605", ink="#ff9a85", muted="#c06a5a", line="#3a1712",
                   accent="#ff6a4d", accent_ink="#140302", danger="#ffb4a3", danger_bg="#2a0905",
                   warn="#ffb08a", warn_bg="#2a1206", ok="#e8a080", ok_bg="#1f0c06", od="#ff7a5c", notice="#ff9f8a",
                   unknown="#c46a1a", unknown_ink="#140700", nkda="#6a3a32", header="#0a0403", header_ink="#ff9a85"),
    },
    "contrast": {
        "name": "High contrast", "blurb": "Maximum contrast for bright sun or low vision.",
        "light": _p(_COMMON_LIGHT, bg="#ffffff", panel="#ffffff", ink="#000000", muted="#2b2b2b", line="#000000",
                    accent="#0033cc", accent_ink="#ffffff", danger="#a00000", danger_solid="#a00000", allergy="#a00000",
                    warn="#5c3b00", ok="#005a1e", od="#a00000", notice="#0033cc", nkda="#333333",
                    header="#000000", header_ink="#ffffff"),
        "dark": _p(_COMMON_DARK, bg="#000000", panel="#000000", ink="#ffffff", muted="#e0e0e0", line="#ffffff",
                   accent="#ffd400", accent_ink="#000000", danger="#ff8080", danger_solid="#b00000", allergy="#b00000",
                   warn="#ffd400", ok="#7dff9a", od="#ff9a70", notice="#9ecbff", unknown="#ffd400", nkda="#444444",
                   header="#000000", header_ink="#ffffff"),
    },
    "slate": {
        "name": "Slate", "blurb": "Neutral graphite with an indigo accent.",
        "light": _p(_COMMON_LIGHT, bg="#f2f2f5", panel="#ffffff", ink="#1e1f26", muted="#5d6070", line="#dcdde4",
                    accent="#4b45b8", accent_ink="#ffffff", header="#26272f", header_ink="#f2f2f5"),
        "dark": _p(_COMMON_DARK, bg="#121318", panel="#1b1c23", ink="#e9e9f0", muted="#9fa1b3", line="#30323d",
                   accent="#9d98ff", accent_ink="#0f0d2b", header="#16171d", header_ink="#e9e9f0"),
    },
}
DEFAULT_THEME = "field"


# ------------------------------------------------------------------ colour maths
def _rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _lum(h: str) -> float:
    def ch(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in _rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return "#" + "".join(f"{round((x + (y - x) * t) * 255):02x}" for x, y in zip(ra, rb))


HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def accent_variants(hex_color: str) -> dict:
    """From one brand colour, derive light- and dark-mode accents that stay readable."""
    light = hex_color
    while contrast(light, "#ffffff") < 4.5:
        light = _mix(light, "#000000", 0.12)
    dark = hex_color
    while contrast(dark, "#1b1c23") < 4.5:
        dark = _mix(dark, "#ffffff", 0.12)
    ink = lambda c: "#ffffff" if contrast(c, "#ffffff") >= contrast(c, "#111111") else "#111111"  # noqa: E731
    return {"light": {"accent": light, "accent-ink": ink(light)}, "dark": {"accent": dark, "accent-ink": ink(dark)}}


# Pairs that must stay readable: (foreground, background, minimum ratio)
CHECKS = [("ink", "bg", 7.0), ("ink", "panel", 7.0), ("muted", "panel", 4.5), ("accent", "panel", 4.5),
          ("accent-ink", "accent", 4.5), ("danger", "panel", 4.5), ("danger", "danger-bg", 4.5),
          ("danger-ink", "danger-solid", 4.5), ("allergy-ink", "allergy", 4.5), ("unknown-ink", "unknown", 4.5),
          ("nkda-ink", "nkda", 4.5), ("warn", "warn-bg", 4.5), ("ok", "ok-bg", 4.5), ("header-ink", "header", 7.0)]


def contrast_report() -> list[str]:
    problems = []
    for key, t in THEMES.items():
        for mode in ("light", "dark"):
            pal = t[mode]
            for fg, bg, need in CHECKS:
                r = contrast(pal[fg], pal[bg])
                if r < need:
                    problems.append(f"{key}/{mode}: {fg} on {bg} = {r:.2f} (needs {need})")
    return problems


# ------------------------------------------------------------------ CSS
def _block(sel: str, pal: dict, scheme: str) -> str:
    body = "; ".join(f"--{k}: {pal[k]}" for k in TOKENS)
    return f"{sel} {{ {body}; color-scheme: {scheme}; }}"


@functools.lru_cache(maxsize=16)
def css(accent: str = "") -> str:
    out = ["/* generated by drprepper.server.themes */"]
    for key, t in THEMES.items():
        base = f'[data-theme="{key}"]'          # any element, so theme previews can be drawn inside a page
        out.append(_block(base, t["light"], "light"))
        out.append(_block(f'{base}[data-mode="dark"]', t["dark"], "dark"))
        out.append("@media (prefers-color-scheme: dark) { " + _block(f'{base}[data-mode="auto"]', t["dark"], "dark") + " }")
    if accent and HEX.match(accent):
        v = accent_variants(accent)
        lt = "; ".join(f"--{k}: {c}" for k, c in v["light"].items())
        dk = "; ".join(f"--{k}: {c}" for k, c in v["dark"].items())
        out.append(f"[data-brand-accent] {{ {lt}; }}")
        out.append(f'[data-brand-accent][data-mode="dark"] {{ {dk}; }}')
        out.append(f'@media (prefers-color-scheme: dark) {{ [data-brand-accent][data-mode="auto"] {{ {dk}; }} }}')
    return "\n".join(out) + "\n"


def css_version(accent: str = "") -> str:
    return hashlib.sha256(css(accent).encode()).hexdigest()[:10]


def resolve(theme: str | None, mode: str | None, default_theme: str, default_mode: str) -> tuple[str, str]:
    t = theme if theme in THEMES else (default_theme if default_theme in THEMES else DEFAULT_THEME)
    m = mode if mode in MODES else (default_mode if default_mode in MODES else "auto")
    return t, m
