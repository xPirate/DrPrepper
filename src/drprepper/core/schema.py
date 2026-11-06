"""Schema for the read-only drug reference database (drugs.db)."""

SCHEMA_VERSION = "1"

DRUG_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE drugs(
  id INTEGER PRIMARY KEY,
  generic TEXT NOT NULL,          -- normalized lower-case generic name
  display TEXT NOT NULL,          -- human display name
  brands TEXT,                    -- ' | ' separated
  substances TEXT,                -- ' | ' separated active substances
  route TEXT,
  kind TEXT,                      -- 'RX' or 'OTC'
  pharm_class TEXT,               -- ' | ' separated EPC/MoA classes
  rxcui TEXT,
  set_id TEXT,
  effective TEXT,                 -- label effective date YYYYMMDD
  label_count INTEGER,            -- how many manufacturer labels were merged
  manufacturer TEXT
);
CREATE INDEX drugs_generic ON drugs(generic);

CREATE TABLE sections(
  drug_id INTEGER NOT NULL,
  ord INTEGER NOT NULL,
  key TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  PRIMARY KEY(drug_id, key)
);

CREATE TABLE tox(
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  ord INTEGER NOT NULL,
  aka TEXT,
  summary TEXT,
  body_json TEXT NOT NULL,        -- [{"title":..., "items":[...]}, ...]
  review_status TEXT
);

CREATE TABLE drug_tox(
  drug_id INTEGER NOT NULL,
  tox_id INTEGER NOT NULL,
  PRIMARY KEY(drug_id, tox_id)
);

CREATE VIRTUAL TABLE drug_fts USING fts5(
  generic, brands, substances, pharm_class, indications,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE tox_fts USING fts5(
  name, aka, summary, body,
  tokenize = 'unicode61 remove_diacritics 2'
);
"""

# Label sections we keep, in the order a responder reads them.
SECTION_ORDER: list[tuple[str, str]] = [
    ("boxed_warning", "Boxed Warning"),
    ("indications_and_usage", "Indications & Usage"),
    ("purpose", "Purpose"),
    ("dosage_and_administration", "Dosage & Administration"),
    ("directions", "Directions"),
    ("dosage_forms_and_strengths", "Dosage Forms & Strengths"),
    ("contraindications", "Contraindications"),
    ("do_not_use", "Do Not Use"),
    ("overdosage", "Overdosage"),
    ("warnings_and_cautions", "Warnings & Precautions"),
    ("warnings", "Warnings"),
    ("drug_interactions", "Drug Interactions"),
    ("adverse_reactions", "Adverse Reactions"),
    ("use_in_specific_populations", "Specific Populations"),
    ("pregnancy", "Pregnancy"),
    ("pediatric_use", "Pediatric Use"),
    ("geriatric_use", "Geriatric Use"),
    ("active_ingredient", "Active Ingredient"),
    ("stop_use", "Stop Use"),
    ("ask_doctor", "Ask a Doctor"),
    ("ask_doctor_or_pharmacist", "Ask Doctor/Pharmacist"),
    ("storage_and_handling", "Storage & Handling"),
    ("how_supplied", "How Supplied"),
    ("drug_abuse_and_dependence", "Abuse & Dependence"),
    ("mechanism_of_action", "Mechanism of Action"),
    ("clinical_pharmacology", "Clinical Pharmacology"),
]
SECTION_TITLES = dict(SECTION_ORDER)
SECTION_RANK = {k: i for i, (k, _) in enumerate(SECTION_ORDER)}
