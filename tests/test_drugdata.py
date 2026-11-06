"""Sample-vs-full drug data: background build, live switch-over, status and warning banner."""
import json
import os
import time
from importlib import resources
from pathlib import Path

import pytest

from drprepper.core.db import LiveDrugDB
from drprepper.ingest.__main__ import is_sample_db, main as ingest

FIXTURE = Path(str(resources.files("drprepper") / "data" / "fixtures" / "openfda_sample.json"))


def build_full(out: Path, cache: Path):
    """Stand-in for the full FDA build: same labels, but not marked as the sample."""
    ingest(["--out", str(out), "--cache", str(cache), "--labels", str(FIXTURE)])


def test_live_db_switches_to_new_build(tmp_path):
    db = tmp_path / "drugs.db"
    ingest(["--out", str(db), "--sample"])
    live = LiveDrugDB(db)
    assert live.is_sample and is_sample_db(db)
    gen = live.generation
    time.sleep(0.01)
    build_full(db, tmp_path / "cache")
    live.refresh(force=True)
    assert live.generation == gen + 1
    assert not live.is_sample and not is_sample_db(db)
    assert live.search("narcan").drugs
    st = live.ingest_status()
    assert st["state"] == "done" and "medications" in st["detail"]


def test_if_sample_skips_when_full_installed(tmp_path, capsys):
    db = tmp_path / "drugs.db"
    build_full(db, tmp_path / "cache")
    before = db.stat().st_mtime_ns
    ingest(["--out", str(db), "--cache", str(tmp_path / "cache"), "--if-sample"])
    assert db.stat().st_mtime_ns == before
    assert "nothing to do" in capsys.readouterr().out


def test_failed_download_writes_status_and_keeps_sample(tmp_path, monkeypatch):
    db = tmp_path / "drugs.db"
    ingest(["--out", str(db), "--sample"])
    import drprepper.ingest.openfda as of

    def offline(cache, progress=None):
        raise ConnectionError("Name or service not known")
    monkeypatch.setattr(of, "download_all", offline)
    with pytest.raises(SystemExit):
        ingest(["--out", str(db), "--cache", str(tmp_path / "cache"), "--if-sample"])
    st = json.loads((tmp_path / "ingest-status.json").read_text())
    assert st["state"] == "failed" and "internet" in st["detail"]
    assert is_sample_db(db)


def test_progress_is_reported(tmp_path, monkeypatch):
    db = tmp_path / "drugs.db"
    import drprepper.ingest.openfda as of
    seen = []

    def fake_download(cache, progress=None):
        for i in (1, 2):
            progress(i, 2, f"part{i}.zip", 120.0)
            seen.append(json.loads((tmp_path / "ingest-status.json").read_text()))
        return [FIXTURE]
    monkeypatch.setattr(of, "download_all", fake_download)
    ingest(["--out", str(db), "--cache", str(tmp_path / "cache")])
    assert seen[0]["detail"] == "Downloading FDA label file 1 of 2 (120 MB)" and seen[1]["step"] == 2
    assert not is_sample_db(db)


def test_console_banner_follows_the_data(app, new_browser, settings):
    """The fixture app runs on the sample: banner shows; after a full build lands, it disappears."""
    b = new_browser()
    page = b.get("/drugs").text
    assert "SAMPLE DRUG DATA" in page and "has not been downloaded" in page
    assert b.get("/healthz").json()["drug_data"] == "sample"

    # simulate the background build in progress
    status = settings.drugs_db.with_name("ingest-status.json")
    status.write_text(json.dumps({"state": "downloading", "detail": "Downloading FDA label file 3 of 13 (120 MB)"}))
    assert "Downloading FDA label file 3 of 13" in b.get("/drugs").text

    # build lands (written to a copy so the shared session fixture db stays the sample)
    full = settings.drugs_db.parent / "full.db"
    build_full(full, settings.drugs_db.parent / "cache")
    live = app.state.drugs
    live.path = full
    live.refresh(force=True)
    page = b.get("/drugs?q=narcan").text
    assert "SAMPLE DRUG DATA" not in page and "Naloxone" in page
    assert b.get("/healthz").json()["drug_data"] == "full"
    status.unlink()
