"""Text helpers shared by ingest, server and touch UI."""
from __future__ import annotations

import re

_WS = re.compile(r"[ \t\r\f\v]+")
_SUBSECTION = re.compile(r"\s(?=\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\s+[A-Z])")
_BULLET = re.compile(r"\s*[•●▪]\s*")
_SMALL = {"and", "or", "of", "in", "with", "for", "the", "to", "a"}
_UNITS = re.compile(r"\b(Mg|Mcg|Ml|Ml/Kg|Mg/Ml|Iu|Usp|Hcl|Er|Xr|Sr|Dr|Odt|Iv|Im)\b")


_HEADINGS = {
    "warnings_and_cautions": r"warnings\s+and\s+precautions",
    "do_not_use": r"do\s+not\s+use",
    "ask_doctor": r"ask\s+a\s+doctor(\s+before\s+use)?(\s+if(\s+you\s+have)?)?",
    "ask_doctor_or_pharmacist": r"ask\s+a\s+doctor\s+or\s+pharmacist(\s+before\s+use)?(\s+if\s+you\s+are)?",
    "stop_use": r"stop\s+use(\s+and\s+ask\s+a\s+doctor(\s+if)?)?",
    "how_supplied": r"how\s+supplied(\s*/\s*storage\s+and\s+handling)?",
    "use_in_specific_populations": r"use\s+in\s+specific\s+populations",
    "active_ingredient": r"active\s+ingredients?(\s*\([^)]{0,40}\))?",
    "boxed_warning": r"(boxed\s+)?warning:?",
}


def _heading_pattern(key: str) -> str:
    if key in _HEADINGS:
        return _HEADINGS[key]
    return r"\s+".join(f"{re.escape(w)}s?" for w in key.split("_"))


def clean_section(raw: str, key: str | None = None) -> str:
    """Normalize a label section: strip the repeated heading, collapse whitespace,
    add paragraph breaks before numbered subsections and bullets."""
    text = _WS.sub(" ", raw.replace("\n", " ")).strip()
    if key:
        pat = rf"^(\d+(\.\d+)?\s+)?{_heading_pattern(key)}\s*[:.]?\s+"
        stripped = re.sub(pat, "", text, count=1, flags=re.IGNORECASE)
        if len(stripped) > 3:
            text = stripped
    text = _SUBSECTION.sub("\n\n", text)
    text = _BULLET.sub("\n• ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def display_name(generic: str) -> str:
    words = generic.lower().split()
    out = [w if (i and w in _SMALL) else w.capitalize() for i, w in enumerate(words)]
    s = " ".join(out)
    return _UNITS.sub(lambda m: m.group(1).upper(), s)


def norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 /,\-]", " ", s.lower())).strip()


def chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    """Split long text into paragraph chunks small enough to render as a texture."""
    chunks: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        while len(para) > max_chars:
            cut = para.rfind(". ", 0, max_chars)
            if cut < max_chars // 3:
                cut = para.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            chunks.append(para[: cut + 1].strip())
            para = para[cut + 1 :].strip()
        if para:
            chunks.append(para)
    return chunks


def fts_query(q: str) -> str | None:
    tokens = re.findall(r"\w+", q.lower())
    if not tokens:
        return None
    return " ".join(f'"{t}"*' for t in tokens[:8])
