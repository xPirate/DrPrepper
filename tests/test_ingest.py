from drprepper.core.db import DrugDB


def test_sample_build_dedupes_and_filters(drugs_db):
    db = DrugDB(drugs_db)
    meta = db.meta()
    assert meta["schema_version"] == "1"
    names = {r["generic"] for r in db.conn.execute("SELECT generic FROM drugs")}
    assert "naloxone hydrochloride" in names
    assert not any("homeopathic" in n for n in names)


def test_brand_and_street_name_search(drugs_db):
    db = DrugDB(drugs_db)
    assert any("naloxone" in d.generic for d in db.search("narcan").drugs)
    assert any(t.slug == "stimulants" for t in db.search("molly").tox)
    assert any(t.slug == "xylazine" for t in db.search("tranq").tox)


def test_drug_links_to_tox_card(drugs_db):
    db = DrugDB(drugs_db)
    hit = next(d for d in db.search("acetaminophen", include_tox=False).drugs)
    d = db.get_drug(hit.id)
    assert any(t["slug"] == "acetaminophen" for t in d["tox"])


def test_all_cards_have_sections(drugs_db):
    db = DrugDB(drugs_db)
    cards = db.list_tox()
    assert len(cards) >= 20
    for c in cards:
        t = db.get_tox(c.slug)
        assert t["sections"] and all(s["items"] for s in t["sections"]), c.slug
