"""Build drugs.db from openFDA label records + curated toxicology cards.

Strategy
  1. Stage every usable label into a temporary SQLite table keyed by
     (generic name, route, Rx/OTC). Thousands of manufacturers ship the same
     generic, so the raw export has huge duplication.
  2. Keep one representative label per key: the one with the most clinical
     sections, newest effective date as tie-breaker. Merge brand names and
     count the labels merged.
  3. Write final tables + FTS indexes, link drugs to toxicology cards.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..core.schema import DRUG_SCHEMA, SCHEMA_VERSION, SECTION_ORDER, SECTION_RANK, SECTION_TITLES
from ..core.text import clean_section, display_name, norm_key
from .curated import load_tox_cards, write_tox

log = logging.getLogger(__name__)

KINDS = {"HUMAN PRESCRIPTION DRUG": "RX", "HUMAN OTC DRUG": "OTC"}
MAX_BRANDS = 40


def _first(lbl: dict, key: str) -> str:
    v = lbl.get(key)
    if isinstance(v, list):
        return " ".join(x for x in v if isinstance(x, str))
    return v if isinstance(v, str) else ""


def _is_homeopathic(lbl: dict) -> bool:
    probe = " ".join(_first(lbl, k) for k in ("purpose", "indications_and_usage", "package_label_principal_display_panel", "description"))[:4000]
    return "homeopath" in probe.lower() or "HPUS" in probe


def stage_labels(labels: Iterable[dict], stage: sqlite3.Connection, stats: Counter) -> None:
    stage.execute("""CREATE TABLE IF NOT EXISTS stage(
        key TEXT, generic TEXT, brands TEXT, substances TEXT, route TEXT, kind TEXT,
        pharm_class TEXT, rxcui TEXT, set_id TEXT, effective TEXT, manufacturer TEXT,
        nsec INTEGER, sections TEXT)""")
    batch = []
    for lbl in labels:
        stats["seen"] += 1
        of = lbl.get("openfda") or {}
        kind = KINDS.get((of.get("product_type") or [""])[0])
        if not kind:
            stats["skip_not_human_rx_otc"] += 1
            continue
        generic = norm_key(" ".join(of.get("generic_name") or []))
        if not generic:
            stats["skip_no_generic"] += 1
            continue
        if _is_homeopathic(lbl):
            stats["skip_homeopathic"] += 1
            continue
        sections = {}
        for key, _title in SECTION_ORDER:
            raw = _first(lbl, key)
            if raw and len(raw) > 10:
                sections[key] = clean_section(raw, key)
        clinical = sum(1 for k in ("indications_and_usage", "purpose", "dosage_and_administration", "directions",
                                   "contraindications", "overdosage", "warnings", "warnings_and_cautions") if k in sections)
        if clinical == 0:
            stats["skip_no_clinical_text"] += 1
            continue
        route = ", ".join(sorted({r.lower() for r in of.get("route") or []})) or "unspecified"
        pharm = sorted({c for k in ("pharm_class_epc", "pharm_class_moa", "pharm_class_cs") for c in of.get(k) or []})
        batch.append((
            f"{generic}|{route}|{kind}", generic,
            json.dumps(sorted({b.strip().title() for b in of.get("brand_name") or [] if b.strip()})),
            " | ".join(sorted({s.lower() for s in of.get("substance_name") or []})),
            route, kind, " | ".join(pharm),
            (of.get("rxcui") or [""])[0], lbl.get("set_id", ""), lbl.get("effective_time", ""),
            (of.get("manufacturer_name") or [""])[0],
            len(sections), json.dumps(sections),
        ))
        stats["staged"] += 1
        if len(batch) >= 2000:
            stage.executemany("INSERT INTO stage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch.clear()
            log.info("  staged %d / seen %d", stats["staged"], stats["seen"])
    if batch:
        stage.executemany("INSERT INTO stage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    stage.commit()


def write_db(stage: sqlite3.Connection, out: Path, tox_yaml: Path | None, sources: list[str], stats: Counter) -> None:
    tmp_out = out.with_suffix(".building")
    tmp_out.unlink(missing_ok=True)
    db = sqlite3.connect(tmp_out)
    db.executescript(DRUG_SCHEMA)

    brands_by_key: dict[str, set[str]] = {}
    counts: Counter = Counter()
    for key, brands in stage.execute("SELECT key, brands FROM stage"):
        brands_by_key.setdefault(key, set()).update(json.loads(brands))
        counts[key] += 1

    best = stage.execute("""
        SELECT * FROM (
          SELECT *, ROW_NUMBER() OVER (PARTITION BY key ORDER BY nsec DESC, effective DESC) AS rn FROM stage
        ) WHERE rn = 1 ORDER BY generic, route, kind""")
    cols = [c[0] for c in best.description]
    n = 0
    for row in best:
        r = dict(zip(cols, row))
        brands = sorted(brands_by_key[r["key"]])
        brands = [b for b in brands if b.lower() != r["generic"]][:MAX_BRANDS]
        cur = db.execute(
            """INSERT INTO drugs(generic, display, brands, substances, route, kind, pharm_class, rxcui,
                                 set_id, effective, label_count, manufacturer)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["generic"], display_name(r["generic"]), " | ".join(brands), r["substances"], r["route"], r["kind"],
             r["pharm_class"], r["rxcui"], r["set_id"], r["effective"], counts[r["key"]], r["manufacturer"]),
        )
        did = cur.lastrowid
        secs = json.loads(r["sections"])
        db.executemany(
            "INSERT INTO sections(drug_id, ord, key, title, body) VALUES (?,?,?,?,?)",
            [(did, SECTION_RANK[k], k, SECTION_TITLES[k], body) for k, body in secs.items()],
        )
        indications = (secs.get("indications_and_usage") or secs.get("purpose") or "")[:2000]
        db.execute(
            "INSERT INTO drug_fts(rowid, generic, brands, substances, pharm_class, indications) VALUES (?,?,?,?,?,?)",
            (did, r["generic"], " ".join(brands), r["substances"], r["pharm_class"], indications),
        )
        n += 1
    stats["drugs"] = n
    stats["rx"] = db.execute("SELECT count(*) FROM drugs WHERE kind='RX'").fetchone()[0]
    stats["otc"] = db.execute("SELECT count(*) FROM drugs WHERE kind='OTC'").fetchone()[0]

    if tox_yaml:
        cards, tox_meta = load_tox_cards(tox_yaml)
        stats["tox_cards"], stats["drug_tox_links"] = write_tox(db, cards)
    else:
        tox_meta = {}

    meta = {
        "schema_version": SCHEMA_VERSION,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "sources": " ; ".join(sources),
        "stats": json.dumps(dict(stats)),
        "disclaimer": tox_meta.get("disclaimer", ""),
        "references": json.dumps(tox_meta.get("references", [])),
    }
    db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    db.commit()
    db.execute("INSERT INTO drug_fts(drug_fts) VALUES('optimize')")
    db.execute("INSERT INTO tox_fts(tox_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    tmp_out.replace(out)


def build(label_files: list[Path], out: Path, tox_yaml: Path | None, work: Path, sources: list[str]) -> Counter:
    from .openfda import iter_labels

    t0 = time.time()
    stats: Counter = Counter()
    work.mkdir(parents=True, exist_ok=True)
    stage_path = work / "stage.sqlite"
    stage_path.unlink(missing_ok=True)
    stage = sqlite3.connect(stage_path)
    stage.execute("PRAGMA journal_mode=OFF")
    stage.execute("PRAGMA synchronous=OFF")
    for p in label_files:
        log.info("reading %s", p.name)
        stage_labels(iter_labels(p), stage, stats)
    log.info("dedupe + write %s", out)
    write_db(stage, out, tox_yaml, sources, stats)
    stage.close()
    stage_path.unlink(missing_ok=True)
    stats["seconds"] = int(time.time() - t0)
    return stats
