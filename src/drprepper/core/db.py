"""Read-only access to drugs.db, shared by the touchscreen app and the server."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .text import fts_query


def default_drugs_db_path() -> Path:
    return Path(os.environ.get("DRP_DRUGS_DB", "/var/lib/drprepper/drugs.db"))


@dataclass
class DrugHit:
    id: int
    display: str
    generic: str
    brands: str
    route: str
    kind: str
    pharm_class: str


@dataclass
class ToxHit:
    slug: str
    name: str
    category: str
    summary: str
    aka: str = ""


@dataclass
class SearchResult:
    tox: list[ToxHit] = field(default_factory=list)
    drugs: list[DrugHit] = field(default_factory=list)


class DrugDB:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else default_drugs_db_path()
        if not self.path.exists():
            raise FileNotFoundError(f"drug database not found: {self.path}")
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    # ---------- metadata ----------
    def meta(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM meta")}

    # ---------- search ----------
    def search(self, q: str, limit: int = 60, kind: str | None = None, include_tox: bool = True) -> SearchResult:
        res = SearchResult()
        q = q.strip()
        match = fts_query(q)
        if not match:
            return res
        ql = q.lower()

        if include_tox:
            rows = self.conn.execute(
                """SELECT t.slug, t.name, t.category, t.summary, t.aka, bm25(tox_fts, 10.0, 8.0, 2.0, 0.5) AS s
                   FROM tox_fts JOIN tox t ON t.id = tox_fts.rowid
                   WHERE tox_fts MATCH ? ORDER BY s LIMIT 6""",
                (match,),
            ).fetchall()
            res.tox = [ToxHit(r["slug"], r["name"], r["category"], r["summary"] or "", r["aka"] or "") for r in rows]

        if kind == "TOX":
            return res

        sql = """SELECT d.id, d.display, d.generic, d.brands, d.route, d.kind, d.pharm_class,
                        bm25(drug_fts, 12.0, 8.0, 4.0, 2.0, 0.5) AS s
                 FROM drug_fts JOIN drugs d ON d.id = drug_fts.rowid
                 WHERE drug_fts MATCH ?"""
        args: list = [match]
        if kind in ("RX", "OTC"):
            sql += " AND d.kind = ?"
            args.append(kind)
        sql += " ORDER BY s LIMIT ?"
        args.append(limit * 3)
        rows = self.conn.execute(sql, args).fetchall()

        def rank(r: sqlite3.Row):
            g = r["generic"]
            brands = (r["brands"] or "").lower().split(" | ")
            if g == ql or ql in brands:
                tier = 0
            elif g.startswith(ql) or any(b.startswith(ql) for b in brands):
                tier = 1
            else:
                tier = 2
            return (tier, r["s"], len(g))

        rows = sorted(rows, key=rank)[:limit]
        res.drugs = [self._hit(r) for r in rows]
        return res

    def suggest(self, q: str, limit: int = 10) -> list[DrugHit]:
        return self.search(q, limit=limit, include_tox=False).drugs

    def browse(self, letter: str, kind: str | None = None, limit: int = 2000) -> list[DrugHit]:
        sql = "SELECT id, display, generic, brands, route, kind, pharm_class FROM drugs WHERE generic >= ? AND generic < ?"
        lo = letter.lower()
        hi = chr(ord(lo) + 1)
        args: list = [lo, hi]
        if kind in ("RX", "OTC"):
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY generic, route LIMIT ?"
        args.append(limit)
        return [self._hit(r) for r in self.conn.execute(sql, args)]

    # ---------- detail ----------
    def get_drug(self, drug_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM drugs WHERE id = ?", (drug_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["sections"] = [
            dict(s) for s in self.conn.execute(
                "SELECT key, title, body FROM sections WHERE drug_id = ? ORDER BY ord", (drug_id,)
            )
        ]
        d["tox"] = [
            dict(t) for t in self.conn.execute(
                """SELECT t.slug, t.name, t.category FROM drug_tox dt JOIN tox t ON t.id = dt.tox_id
                   WHERE dt.drug_id = ? ORDER BY t.ord""",
                (drug_id,),
            )
        ]
        return d

    def list_tox(self) -> list[ToxHit]:
        return [
            ToxHit(r["slug"], r["name"], r["category"], r["summary"] or "", r["aka"] or "")
            for r in self.conn.execute("SELECT slug, name, category, summary, aka FROM tox ORDER BY ord")
        ]

    def get_tox(self, slug: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM tox WHERE slug = ?", (slug,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["sections"] = json.loads(d.pop("body_json"))
        return d

    @staticmethod
    def _hit(r: sqlite3.Row) -> DrugHit:
        return DrugHit(r["id"], r["display"], r["generic"], r["brands"] or "", r["route"] or "", r["kind"] or "", r["pharm_class"] or "")


class LiveDrugDB:
    """A DrugDB that follows its file: when a new build replaces drugs.db (for example the
    full FDA download finishing in the background), the next call reopens it."""

    CHECK_EVERY = 2.0  # seconds between file checks

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else default_drugs_db_path()
        self._db: DrugDB | None = None
        self._sig = None
        self._checked = 0.0
        self._meta: dict[str, str] = {}
        self.generation = 0          # bumps each time a new file is opened
        self.refresh(force=True)

    def _signature(self):
        try:
            st = self.path.stat()
            return (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def refresh(self, force: bool = False) -> bool:
        """Reopen if the file changed. Returns True when a new database was opened."""
        import time
        now = time.monotonic()
        if not force and now - self._checked < self.CHECK_EVERY:
            return False
        self._checked = now
        sig = self._signature()
        if sig == self._sig and self._db is not None:
            return False
        if sig is None:
            self._db, self._sig, self._meta = None, None, {}
            return False
        try:
            db = DrugDB(self.path)
            meta = db.meta()
        except (sqlite3.Error, FileNotFoundError):
            return False           # half-written or locked; try again on the next check
        old, self._db, self._sig, self._meta = self._db, db, sig, meta
        self.generation += 1
        if old is not None:
            try:
                old.conn.close()
            except sqlite3.Error:
                pass
        return True

    def __bool__(self) -> bool:
        self.refresh()
        return self._db is not None

    def __getattr__(self, name):
        self.refresh()
        if self._db is None:
            raise FileNotFoundError(f"drug database not found: {self.path}")
        return getattr(self._db, name)

    def meta(self) -> dict[str, str]:
        self.refresh()
        return dict(self._meta)

    @property
    def is_sample(self) -> bool:
        self.refresh()
        return self._db is None or "SAMPLE" in self._meta.get("sources", "")

    def ingest_status(self) -> dict | None:
        """Progress of a background full build, if one has run (ingest-status.json)."""
        p = self.path.with_name("ingest-status.json")
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None
