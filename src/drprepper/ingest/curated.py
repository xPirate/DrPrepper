"""Load curated toxicology / overdose cards and link them to label drugs."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import yaml

REQUIRED = ("slug", "name", "category", "summary", "sections")


def load_tox_cards(path: Path) -> tuple[list[dict], dict]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cards = doc["cards"]
    seen = set()
    for c in cards:
        missing = [k for k in REQUIRED if not c.get(k)]
        if missing:
            raise ValueError(f"tox card {c.get('slug')!r} missing {missing}")
        if c["slug"] in seen:
            raise ValueError(f"duplicate tox slug {c['slug']}")
        seen.add(c["slug"])
        for s in c["sections"]:
            if not s.get("title") or not s.get("items"):
                raise ValueError(f"tox card {c['slug']}: section needs title + items")
    return cards, doc.get("meta", {})


def write_tox(db: sqlite3.Connection, cards: list[dict]) -> tuple[int, int]:
    links = 0
    drugs = db.execute("SELECT id, generic, pharm_class, substances FROM drugs").fetchall()
    for i, c in enumerate(cards):
        body = [{"title": s["title"], "items": [str(x).strip() for x in s["items"]]} for s in c["sections"]]
        aka = " | ".join(c.get("aka") or [])
        cur = db.execute(
            "INSERT INTO tox(slug, name, category, ord, aka, summary, body_json, review_status) VALUES (?,?,?,?,?,?,?,?)",
            (c["slug"], c["name"], c["category"], i, aka, c["summary"], json.dumps(body), c.get("review_status", "draft")),
        )
        tid = cur.lastrowid
        flat = "\n".join(f"{s['title']}: " + " ".join(s["items"]) for s in body)
        db.execute("INSERT INTO tox_fts(rowid, name, aka, summary, body) VALUES (?,?,?,?,?)",
                   (tid, c["name"], aka.replace("|", " "), c["summary"], flat))

        match = c.get("match") or {}
        classes = [m.lower() for m in match.get("classes", [])]
        generics = [m.lower() for m in match.get("generics", [])]
        gen_re = re.compile(r"\b(" + "|".join(map(re.escape, generics)) + r")\b") if generics else None
        for d in drugs:
            pc = (d[2] or "").lower()
            names = f"{d[1]} {d[3] or ''}"
            if any(cl in pc for cl in classes) or (gen_re and gen_re.search(names)):
                db.execute("INSERT OR IGNORE INTO drug_tox VALUES (?, ?)", (d[0], tid))
                links += 1
    return len(cards), links
