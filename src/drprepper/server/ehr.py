"""Encrypted EHR storage (SQLCipher), schema migrations and data-access helpers."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .config import write_private, Settings, ensure_secret

try:  # SQLCipher (AES-256 page encryption)
    from sqlcipher3 import dbapi2 as sqlcipher  # type: ignore
except ImportError:  # pragma: no cover
    sqlcipher = None

SCHEMA_VERSION = 8
ROLES = ("admin", "triage", "provider", "viewer")
ROLE_LABELS = {"admin": "Admin", "triage": "Triage", "provider": "Provider", "viewer": "Viewer (read only)"}
CREDENTIALS = ["EMR", "EMT-B", "AEMT", "EMT-P", "FP-C", "CCP", "LPN", "RN", "NP", "PA", "DO", "MD", "Medic (military)", "Other"]

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL COLLATE NOCASE,
  last_name TEXT NOT NULL DEFAULT '',
  first_name TEXT NOT NULL DEFAULT '',
  credential TEXT NOT NULL DEFAULT '',
  full_name TEXT NOT NULL,                -- display form "Last, First CRED", kept in sync
  role TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  must_change_pw INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_login_at TEXT,
  theme TEXT, mode TEXT                   -- display preference; NULL = site default
);
CREATE TABLE IF NOT EXISTS patients(
  id INTEGER PRIMARY KEY,
  pnum TEXT UNIQUE NOT NULL,              -- 8-digit patient number
  mrn TEXT,                               -- legacy (v1) identifier
  last_name TEXT, first_name TEXT,
  dob TEXT, age_est TEXT, sex TEXT,
  weight_kg REAL,
  allergies TEXT, conditions TEXT, home_meds TEXT,
  contact TEXT, identifying TEXT,
  assigned_to INTEGER REFERENCES users(id), assigned_at TEXT,
  last_seen_by INTEGER REFERENCES users(id), last_seen_at TEXT,
  incoming INTEGER NOT NULL DEFAULT 0,    -- pre-arrival report not yet arrived
  eta_at TEXT, arrived_at TEXT, intake_source TEXT,
  created_at TEXT NOT NULL, created_by INTEGER, updated_at TEXT,
  status TEXT NOT NULL DEFAULT 'active',  -- active | discharged | ama | transfer | lwbs | deceased | did_not_arrive
  location_id INTEGER REFERENCES locations(id), location_at TEXT,
  queued_at TEXT,                         -- waiting for a provider since (NULL once assigned)
  triage_start_at TEXT, triage_done_at TEXT,   -- first triage: arrival -> first assignment
  disposition_at TEXT,
  surgical_hx TEXT, family_hx TEXT, social_tobacco TEXT, social_alcohol TEXT, social_substances TEXT,
  social_notes TEXT, pregnancy TEXT, lmp TEXT, tetanus TEXT, immunizations TEXT, code_status TEXT,
  advance_directive TEXT, blood_type TEXT, devices TEXT, pcp TEXT, history_updated_at TEXT, history_updated_by INTEGER
);
CREATE TABLE IF NOT EXISTS locations(
  id INTEGER PRIMARY KEY,
  parent_id INTEGER REFERENCES locations(id),   -- a bed inside a room; NULL for a top-level area
  name TEXT NOT NULL COLLATE NOCASE,            -- unique among its siblings
  kind TEXT NOT NULL CHECK (kind IN ('triage','room','or','other','morgue')),
  capacity INTEGER NOT NULL DEFAULT 1,    -- 0 = no limit
  active INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'ready',    -- ready | dirty | cleaning | out (when nobody is in it)
  state_at TEXT, state_by INTEGER
);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,                    -- SHA-256 of the token in the cookie (the token itself is never stored)
  user_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  client TEXT, revoked_at TEXT, revoked_reason TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS revisions(
  id INTEGER PRIMARY KEY,
  entity TEXT NOT NULL,                   -- patient | encounter
  entity_id INTEGER NOT NULL,
  field TEXT NOT NULL,
  old_value TEXT, new_value TEXT,
  at TEXT NOT NULL, by_user INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS revisions_entity ON revisions(entity, entity_id);
CREATE TABLE IF NOT EXISTS display_keys(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  key_hash TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL, created_by INTEGER REFERENCES users(id),
  last_seen_at TEXT, last_client TEXT,
  revoked_at TEXT, revoked_by INTEGER
);
CREATE TABLE IF NOT EXISTS patient_moves(
  id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  at TEXT NOT NULL,
  kind TEXT NOT NULL,                     -- arrive | move | discharge | death | readmit | correct
  from_location TEXT, to_location TEXT,   -- names as they were at the time
  to_location_id INTEGER,
  status TEXT,                            -- patient status after this change
  by_user INTEGER REFERENCES users(id),
  note TEXT
);
CREATE TABLE IF NOT EXISTS dispositions(
  id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  at TEXT NOT NULL,
  kind TEXT NOT NULL,                     -- discharged | ama | transfer | lwbs | deceased
  by_user INTEGER REFERENCES users(id),
  condition TEXT, destination TEXT, instructions TEXT, followup TEXT,
  ama_capacity INTEGER, ama_risks INTEGER, ama_alternatives INTEGER, ama_return INTEGER,
  ama_risks_text TEXT, ama_signature TEXT, witness TEXT,
  time_of_death TEXT, pronounced_by TEXT, death_notes TEXT, nok_notified TEXT, tag_number TEXT, effects TEXT,
  undone_at TEXT, undone_by INTEGER, undo_reason TEXT
);
CREATE TABLE IF NOT EXISTS patient_meds(
  id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  name TEXT NOT NULL, drug_id INTEGER,
  dose TEXT, route TEXT, frequency TEXT, indication TEXT, last_dose TEXT,
  status TEXT NOT NULL DEFAULT 'taking',  -- taking | prn | stopped | unknown | error
  source TEXT, notes TEXT,
  added_at TEXT NOT NULL, added_by INTEGER REFERENCES users(id),
  updated_at TEXT, updated_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS assignments(
  id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  at TEXT NOT NULL,
  from_user INTEGER REFERENCES users(id),
  to_user INTEGER REFERENCES users(id),
  by_user INTEGER REFERENCES users(id),
  kind TEXT NOT NULL,                     -- assign | handoff | unassign
  note TEXT
);
CREATE TABLE IF NOT EXISTS encounters(
  id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  started_at TEXT NOT NULL,
  location TEXT, chief_complaint TEXT,
  history TEXT, exam TEXT, assessment TEXT, plan TEXT, disposition TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  provider_id INTEGER REFERENCES users(id),
  closed_at TEXT
);
CREATE TABLE IF NOT EXISTS vitals(
  id INTEGER PRIMARY KEY,
  encounter_id INTEGER NOT NULL REFERENCES encounters(id),
  taken_at TEXT NOT NULL,
  hr INTEGER, bp_sys INTEGER, bp_dia INTEGER, rr INTEGER, spo2 INTEGER,
  temp_c REAL, gcs INTEGER, glucose INTEGER, pain INTEGER,
  notes TEXT, recorded_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS med_admin(
  id INTEGER PRIMARY KEY,
  encounter_id INTEGER NOT NULL REFERENCES encounters(id),
  given_at TEXT NOT NULL,
  drug_id INTEGER, drug_name TEXT NOT NULL,
  dose TEXT NOT NULL, route TEXT NOT NULL,
  indication TEXT, response TEXT,
  allergy_override INTEGER NOT NULL DEFAULT 0,
  given_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY,
  encounter_id INTEGER NOT NULL REFERENCES encounters(id),
  created_at TEXT NOT NULL,
  author_id INTEGER REFERENCES users(id),
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  user_id INTEGER,
  user_display TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'info',  -- info | notice | warning
  entity TEXT, entity_id INTEGER,
  pnum TEXT,
  reason TEXT,
  detail TEXT,
  client TEXT,
  message TEXT NOT NULL DEFAULT '',
  prev_hash TEXT,
  hash TEXT                               -- NULL until sealed into the chain
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS backup_runs(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, dest TEXT,
  file TEXT, size INTEGER, ok INTEGER NOT NULL, error TEXT, seconds REAL
);
CREATE INDEX IF NOT EXISTS enc_patient ON encounters(patient_id);
CREATE INDEX IF NOT EXISTS vitals_enc ON vitals(encounter_id);
CREATE INDEX IF NOT EXISTS meds_enc ON med_admin(encounter_id);
CREATE INDEX IF NOT EXISTS notes_enc ON notes(encounter_id);
CREATE INDEX IF NOT EXISTS pat_assigned ON patients(assigned_to);
CREATE INDEX IF NOT EXISTS pat_incoming ON patients(incoming);
CREATE INDEX IF NOT EXISTS pat_status ON patients(status);
CREATE UNIQUE INDEX IF NOT EXISTS loc_name ON locations(COALESCE(parent_id, 0), name COLLATE NOCASE);
CREATE VIEW IF NOT EXISTS loc_v AS
  SELECT l.*, CASE WHEN p.id IS NULL THEN l.name ELSE p.name || ' · ' || l.name END AS label,
         p.name AS parent_name, COALESCE(p.sort, l.sort) AS top_sort, COALESCE(l.parent_id, l.id) AS top_id,
         (l.active AND COALESCE(p.active, 1)) AS live,
         (SELECT COUNT(*) FROM locations ch WHERE ch.parent_id = l.id AND ch.active = 1) AS n_children
  FROM locations l LEFT JOIN locations p ON p.id = l.parent_id;
CREATE INDEX IF NOT EXISTS moves_patient ON patient_moves(patient_id);
CREATE INDEX IF NOT EXISTS disp_patient ON dispositions(patient_id);
CREATE INDEX IF NOT EXISTS pmeds_patient ON patient_meds(patient_id);
CREATE INDEX IF NOT EXISTS assign_patient ON assignments(patient_id);
CREATE INDEX IF NOT EXISTS audit_pnum ON audit(pnum);
CREATE INDEX IF NOT EXISTS audit_user ON audit(user_id);
CREATE INDEX IF NOT EXISTS audit_unsealed ON audit(hash) WHERE hash IS NULL;
"""


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class EHR:
    def __init__(self, settings: Settings):
        self.s = settings
        if sqlcipher is None and not settings.allow_plaintext:
            raise RuntimeError("sqlcipher3 is not installed; refusing to store patient data unencrypted. "
                               "Install sqlcipher3-wheels (or set DRP_ALLOW_PLAINTEXT_EHR=1 for development only).")
        self.encrypted = sqlcipher is not None and not settings.allow_plaintext
        self.seal_lock = threading.Lock()
        self.maintenance = threading.Event()   # set during restore: requests get 503
        self.ready = threading.Event()         # cleared while the key is sealed and nobody has unlocked it yet
        self._on_ready: list = []
        self.kek: bytes | None = None          # in memory only, after an unlock
        self.kek_salt: bytes | None = None
        from .keyseal import sealed_path
        if self.encrypted and sealed_path(settings).exists() and not settings.key_file.exists():
            self.key = None                    # LOCKED until /unlock
        else:
            self.key = ensure_secret(settings.key_file) if self.encrypted else None
            self.ready.set()

    # ---------------- unlock-at-start (see keyseal.py)
    @property
    def locked(self) -> bool:
        return not self.ready.is_set()

    def on_ready(self, fn) -> None:
        """Run now if the records are open, otherwise right after the unlock."""
        if self.ready.is_set():
            fn()
        else:
            self._on_ready.append(fn)

    def unlock(self, passphrase: str) -> None:
        from .keyseal import sealed_path, unseal
        with self.seal_lock:
            if self.ready.is_set():
                return
            key, kek, salt = unseal(sealed_path(self.s).read_bytes(), passphrase)
            self.key, self.kek, self.kek_salt = key, kek, salt
            conn = self.connect()                 # prove the key opens this database before declaring it open
            try:
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            finally:
                conn.close()
            self.init_schema()
            self.ready.set()
        for fn in self._on_ready:
            fn()
        self._on_ready.clear()

    def seal_key(self, passphrase: str) -> None:
        """Seal the current key under a (new) passphrase and remove any plain copy."""
        from .keyseal import seal, sealed_path, shred
        blob, kek = seal(self.key, passphrase)
        write_private(sealed_path(self.s), blob)
        self.kek, self.kek_salt = kek, __import__("json").loads(blob)["salt"]
        shred(self.s.key_file)

    def unseal_key(self) -> None:
        from .keyseal import sealed_path, shred
        write_private(self.s.key_file, self.key + "\n")
        shred(sealed_path(self.s))
        self.kek = self.kek_salt = None

    def store_key(self, key_hex: str) -> None:
        """Replace the key (restore). Keeps the current mode: sealed stays sealed under the same passphrase."""
        import base64
        from .keyseal import seal_with, sealed_path
        if sealed_path(self.s).exists() and self.kek:
            salt = self.kek_salt if isinstance(self.kek_salt, bytes) else base64.b64decode(self.kek_salt)
            write_private(sealed_path(self.s), seal_with(key_hex, self.kek, salt))
        else:
            write_private(self.s.key_file, key_hex + "\n")
        self.key = key_hex

    def connect(self, path: Path | None = None, key: str | None = None):
        path = Path(path or self.s.ehr_db)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.encrypted:
            conn = sqlcipher.connect(str(path), check_same_thread=False, timeout=15)
            conn.execute(f"PRAGMA key = \"x'{key or self.key}'\"")
            conn.execute("PRAGMA cipher_memory_security = ON")
        else:
            conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        conn.row_factory = _row_factory
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    @contextmanager
    def session(self) -> Iterator:
        if not self.ready.is_set():
            raise RuntimeError("The EHR is locked: enter the unlock passphrase first.")
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        from .auditlog import seal  # late import: auditlog imports ehr
        seal(self)

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            ver = conn.execute("PRAGMA user_version").fetchone()["user_version"]
            has_users = conn.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone()
            if has_users and ver < 2:
                _migrate_v1_to_v2(conn)
            if has_users:
                _add_columns(conn, "patients", V3_PATIENT_COLUMNS)
                _add_columns(conn, "users", (("theme", "TEXT"), ("mode", "TEXT")))
                _add_columns(conn, "patients", V5_PATIENT_COLUMNS)
                _add_columns(conn, "encounters", (("exam", "TEXT"),))
                _add_columns(conn, "locations", (("state", "TEXT NOT NULL DEFAULT 'ready'"), ("state_at", "TEXT"),
                                                 ("state_by", "INTEGER")))
                _migrate_locations_v7(conn)
            conn.executescript(SCHEMA_V2)
            if has_users and ver < 5:
                _backfill_v5(conn)
            # Assigned patients never get a running triage clock (pre-assigned incoming patients from v5).
            conn.execute("UPDATE patients SET triage_done_at = triage_start_at WHERE triage_done_at IS NULL "
                         "AND triage_start_at IS NOT NULL AND assigned_to IS NOT NULL")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()

    def backup_to(self, dest: Path) -> None:
        """Hot, consistent copy of the EHR. The copy is encrypted with the same key."""
        dest = Path(dest)
        dest.unlink(missing_ok=True)
        src = self.connect()
        try:
            if self.encrypted:
                src.execute(f"ATTACH DATABASE ? AS bk KEY \"x'{self.key}'\"", (str(dest),))
                src.execute("SELECT sqlcipher_export('bk')")
                ver = src.execute("PRAGMA user_version").fetchone()["user_version"]
                src.execute(f"PRAGMA bk.user_version = {ver}")
                src.execute("DETACH DATABASE bk")
            else:
                dst = sqlite3.connect(str(dest))
                src.backup(dst)
                dst.close()
        finally:
            src.close()


def _row_factory(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


V3_PATIENT_COLUMNS = (("incoming", "INTEGER NOT NULL DEFAULT 0"), ("eta_at", "TEXT"), ("arrived_at", "TEXT"),
                      ("intake_source", "TEXT"))


V5_PATIENT_COLUMNS = tuple((n, "TEXT") for n in (
    "location_at", "queued_at", "triage_start_at", "triage_done_at", "disposition_at", "surgical_hx", "family_hx",
    "social_tobacco", "social_alcohol", "social_substances", "social_notes", "pregnancy", "lmp", "tetanus",
    "immunizations", "code_status", "advance_directive", "blood_type", "devices", "pcp", "history_updated_at")) + (
    ("status", "TEXT NOT NULL DEFAULT 'active'"), ("location_id", "INTEGER"), ("history_updated_by", "INTEGER"))

DEFAULT_LOCATIONS = ([("Triage", "triage", 0)] + [(f"Room {i}", "room", 1) for i in range(1, 7)]
                     + [("OR 1", "or", 1), ("OR 2", "or", 1), ("Holding", "other", 0), ("Morgue", "morgue", 0)])


LOC_ORDER = "top_sort, top_id, parent_id IS NOT NULL, sort, id"


def seed_locations(c) -> int:
    """Optional sample layout (Admin > Locations > 'Start from a sample layout'). Nothing is created by default."""
    n = 0
    base = (c.execute("SELECT MAX(sort) AS m FROM locations").fetchone()["m"] or 0) + 10
    for i, (name, kind, cap) in enumerate(DEFAULT_LOCATIONS):
        if c.execute("SELECT 1 FROM locations WHERE parent_id IS NULL AND name = ?", (name,)).fetchone():
            continue
        c.execute("INSERT INTO locations(name, kind, capacity, sort) VALUES (?,?,?,?)", (name, kind, cap, base + i * 10))
        n += 1
    return n


def triage_location(c) -> int | None:
    """Where new and arriving patients are placed: the first triage spot with room, else the first triage spot."""
    rows = c.execute(f"""SELECT v.id, v.capacity,
                            (SELECT COUNT(*) FROM patients p WHERE p.location_id = v.id AND p.status = 'active' AND p.incoming = 0) AS n
                         FROM loc_v v WHERE v.kind = 'triage' AND v.live = 1 AND v.n_children = 0
                         ORDER BY {LOC_ORDER}""").fetchall()
    for r in rows:
        if not r["capacity"] or r["n"] < r["capacity"]:
            return r["id"]
    return rows[0]["id"] if rows else None


def _migrate_locations_v7(c) -> None:
    """v5/v6 locations had globally unique names and no beds. Rebuild the table keeping every id."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(locations)")}
    if not cols or "parent_id" in cols:
        return
    c.execute("PRAGMA foreign_keys = OFF")
    c.executescript("""
    DROP VIEW IF EXISTS loc_v;
    CREATE TABLE locations_v7(
      id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES locations(id), name TEXT NOT NULL COLLATE NOCASE,
      kind TEXT NOT NULL CHECK (kind IN ('triage','room','or','other','morgue')), capacity INTEGER NOT NULL DEFAULT 1,
      active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'ready',
      state_at TEXT, state_by INTEGER);
    INSERT INTO locations_v7(id, name, kind, capacity, active, sort, state, state_at, state_by)
      SELECT id, name, kind, capacity, active, sort, state, state_at, state_by FROM locations;
    DROP TABLE locations;
    ALTER TABLE locations_v7 RENAME TO locations;
    """)
    c.execute("PRAGMA foreign_keys = ON")


def _backfill_v5(c) -> None:
    """Patients from before locations existed: put them in triage and reconstruct triage times."""
    tri = triage_location(c)
    c.execute("""UPDATE patients SET triage_start_at = COALESCE(arrived_at, created_at)
                 WHERE triage_start_at IS NULL AND incoming = 0""")
    c.execute("""UPDATE patients SET triage_done_at = (SELECT MIN(a.at) FROM assignments a WHERE a.patient_id = patients.id
                                                         AND a.to_user IS NOT NULL AND a.at >= patients.triage_start_at)
                 WHERE triage_done_at IS NULL AND triage_start_at IS NOT NULL""")
    c.execute("UPDATE patients SET queued_at = triage_start_at WHERE assigned_to IS NULL AND incoming = 0 AND queued_at IS NULL")
    if tri:
        c.execute("UPDATE patients SET location_id = ?, location_at = COALESCE(arrived_at, created_at) "
                  "WHERE location_id IS NULL AND incoming = 0 AND status = 'active'", (tri,))


def _add_columns(c, table: str, cols) -> None:
    have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    if not have:          # table doesn't exist yet; the schema script creates it complete
        return
    for col, decl in cols:
        if col not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _migrate_v1_to_v2(c) -> None:
    """Upgrade a v1 database (first Docker build) in place."""
    c.execute("PRAGMA foreign_keys = OFF")
    cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
    if "last_name" not in cols:
        c.executescript("""
        CREATE TABLE users_v2(
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
          last_name TEXT NOT NULL DEFAULT '', first_name TEXT NOT NULL DEFAULT '', credential TEXT NOT NULL DEFAULT '',
          full_name TEXT NOT NULL, role TEXT NOT NULL, pw_hash TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, must_change_pw INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, last_login_at TEXT);
        INSERT INTO users_v2(id, username, last_name, full_name, role, pw_hash, active, must_change_pw, created_at)
          SELECT id, username, full_name, full_name, role, pw_hash, active, must_change_pw, created_at FROM users;
        DROP TABLE users;
        ALTER TABLE users_v2 RENAME TO users;
        """)
    pcols = {r["name"] for r in c.execute("PRAGMA table_info(patients)")}
    for col, decl in (("pnum", "TEXT"), ("assigned_to", "INTEGER"), ("assigned_at", "TEXT"),
                      ("last_seen_by", "INTEGER"), ("last_seen_at", "TEXT")):
        if col not in pcols:
            c.execute(f"ALTER TABLE patients ADD COLUMN {col} {decl}")
    for p in c.execute("SELECT id FROM patients WHERE pnum IS NULL").fetchall():
        c.execute("UPDATE patients SET pnum = ? WHERE id = ?", (new_pnum(c), p["id"]))
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS patients_pnum ON patients(pnum)")
    acols = {r["name"] for r in c.execute("PRAGMA table_info(audit)")}
    for col, decl in (("user_display", "TEXT NOT NULL DEFAULT ''"), ("severity", "TEXT NOT NULL DEFAULT 'info'"),
                      ("pnum", "TEXT"), ("reason", "TEXT"), ("client", "TEXT"),
                      ("message", "TEXT NOT NULL DEFAULT ''"), ("prev_hash", "TEXT"), ("hash", "TEXT")):
        if col not in acols:
            c.execute(f"ALTER TABLE audit ADD COLUMN {col} {decl}")
    c.execute("UPDATE audit SET message = at || ' - ' || action || COALESCE(' ' || detail, '') WHERE message = ''")
    c.execute("PRAGMA foreign_keys = ON")


# ------------------------------------------------------------------ identities
def display_name(last: str | None, first: str | None, credential: str | None) -> str:
    """'Smith, Harold EMT-B' — the form used everywhere a person is shown."""
    last, first, credential = (last or "").strip(), (first or "").strip(), (credential or "").strip()
    name = f"{last}, {first}" if last and first else (last or first)
    return f"{name} {credential}".strip()


def user_display(u: dict | None) -> str:
    if not u:
        return "System"
    return u.get("full_name") or u.get("username") or "?"


def new_pnum(c) -> str:
    """Random 8-digit patient number (doesn't reveal how many patients exist)."""
    while True:
        n = str(secrets.randbelow(90_000_000) + 10_000_000)
        if not c.execute("SELECT 1 FROM patients WHERE pnum = ?", (n,)).fetchone():
            return n


def patient_label(p: dict) -> str:
    name = ", ".join(x for x in (p.get("last_name"), p.get("first_name")) if x)
    return name or p.get("identifying") or "Unknown patient"


# ------------------------------------------------------------------ revisions (integrity: earlier values are kept)
def record_revisions(c, entity: str, entity_id: int, old: dict, new: dict, fields, user: dict | None) -> list[str]:
    """Append one row per changed field, in the caller's transaction. Returns the names that changed."""
    t, changed = now(), []
    for k in fields:
        a, b = old.get(k), new.get(k)
        if str(a if a is not None else "") != str(b if b is not None else ""):
            c.execute("INSERT INTO revisions(entity, entity_id, field, old_value, new_value, at, by_user) VALUES (?,?,?,?,?,?,?)",
                      (entity, entity_id, k, None if a is None else str(a), None if b is None else str(b), t,
                       user["id"] if user else None))
            changed.append(k)
    return changed


def revisions(c, entity: str, entity_id: int) -> list[dict]:
    return c.execute("""SELECT r.*, u.full_name AS by_name FROM revisions r LEFT JOIN users u ON u.id = r.by_user
                        WHERE r.entity = ? AND r.entity_id = ? ORDER BY r.id DESC""", (entity, entity_id)).fetchall()


# ------------------------------------------------------------------ settings store
def get_setting(c, key: str, default=None):
    r = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(c, key: str, value) -> None:
    c.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
              (key, json.dumps(value)))


# ------------------------------------------------------------------ allergies
ALLERGY_ALIASES = {
    "pcn": ["penicillin", "amoxicillin", "ampicillin"],
    "penicillin": ["penicillin", "amoxicillin", "ampicillin", "piperacillin", "nafcillin", "dicloxacillin"],
    "sulfa": ["sulfonamide", "sulfamethoxazole", "sulfasalazine"],
    "nsaid": ["nonsteroidal anti-inflammatory", "ibuprofen", "naproxen", "ketorolac", "aspirin", "diclofenac", "meloxicam"],
    "nsaids": ["nonsteroidal anti-inflammatory", "ibuprofen", "naproxen", "ketorolac", "aspirin", "diclofenac", "meloxicam"],
    "asa": ["aspirin", "salicylate"],
    "opiates": ["opioid agonist", "morphine", "codeine", "oxycodone", "hydrocodone", "fentanyl"],
    "opioids": ["opioid agonist", "morphine", "codeine", "oxycodone", "hydrocodone", "fentanyl"],
    "cephalosporin": ["cephalosporin", "cephalexin", "ceftriaxone", "cefazolin"],
    "benzos": ["benzodiazepine"],
    "latex": [],
}
_SPLIT = re.compile(r"[,;/\n]+")
NKDA_TERMS = {"none", "nka", "nkda", "no known allergies", "no known drug allergies", "denies", "nkma"}
UNKNOWN_TERMS = {"unknown", "unk", "n/a", "na", "?", "unable to obtain", "not obtained"}


def allergy_items(allergies: str | None) -> list[str]:
    return [a.strip() for a in _SPLIT.split(allergies or "") if a.strip()]


def allergy_status(allergies: str | None) -> str:
    """'allergies' | 'nkda' | 'unknown' — drives the red / gray / amber badge."""
    items = [a.lower().strip(" .") for a in allergy_items(allergies)]
    if not items or all(a in UNKNOWN_TERMS for a in items):
        return "unknown"
    if all(a in NKDA_TERMS for a in items):
        return "nkda"
    return "allergies"


def allergy_hits(allergies: str | None, drug_terms: str) -> list[str]:
    """Allergy entries that plausibly match the drug (name, brands, class)."""
    if allergy_status(allergies) != "allergies":
        return []
    hay = drug_terms.lower()
    hits = []
    for raw in allergy_items(allergies):
        entry = re.sub(r"\(.*?\)|-.*$", "", raw.lower()).strip()   # drop "(rash)" / "- hives"
        if len(entry) < 3 or entry in NKDA_TERMS or entry in UNKNOWN_TERMS:
            continue
        needles = ALLERGY_ALIASES.get(entry, []) + [entry]
        if any(n and n in hay for n in needles):
            hits.append(raw)
    return hits
