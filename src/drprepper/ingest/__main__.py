"""drprepper-ingest — build drugs.db.

  # Full build (downloads ~1.5 GB of openFDA label partitions into the cache; resumable)
  drprepper-ingest --out data/drugs.db --cache data/cache

  # Offline / CI build from the bundled fixture (no network, placeholder text)
  drprepper-ingest --out data/drugs.db --sample

  # Build from label files you already have (.json.zip partitions or .json)
  drprepper-ingest --out data/drugs.db --labels path/to/drug-label-0001-of-0013.json.zip ...

  # Only build if the current database is missing or the sample (used on first start)
  drprepper-ingest --out data/drugs.db --cache data/cache --if-sample

The finished database replaces the old one in a single step, so the web console and
touchscreen keep working during a build and pick up the new data automatically.
Progress is written to ingest-status.json next to the database (shown on both screens).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from importlib import resources
from pathlib import Path

from .build import build


def status_path_for(out: Path) -> Path:
    return out.with_name("ingest-status.json")


class Status:
    def __init__(self, path: Path | None):
        self.path = path

    def write(self, state: str, detail: str = "", **extra) -> None:
        if not self.path:
            return
        data = {"state": state, "detail": detail, "at": datetime.now().astimezone().isoformat(timespec="seconds"), **extra}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)


def is_sample_db(path: Path) -> bool:
    if not path.exists():
        return True
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = c.execute("SELECT value FROM meta WHERE key = 'sources'").fetchone()
        c.close()
        return bool(row and "SAMPLE" in row[0])
    except sqlite3.Error:
        return True


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="drprepper-ingest", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cache", type=Path, default=Path("data/cache"))
    ap.add_argument("--labels", type=Path, nargs="*", help="local label files instead of downloading")
    ap.add_argument("--sample", action="store_true", help="use bundled fixture (offline test build)")
    ap.add_argument("--tox", type=Path, help="toxicology YAML (default: bundled data/toxicology.yaml)")
    ap.add_argument("--if-sample", action="store_true", help="skip if a real (non-sample) database is already installed")
    ap.add_argument("--status", type=Path, help="progress file (default: ingest-status.json next to --out)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(message)s")

    pkg = resources.files("drprepper") / "data"
    tox = a.tox or Path(str(pkg / "toxicology.yaml"))
    a.out.parent.mkdir(parents=True, exist_ok=True)

    if a.if_sample and not a.sample and not is_sample_db(a.out):
        print(f"{a.out} already holds the full drug database; nothing to do.")
        return

    status = Status(None if a.sample else (a.status or status_path_for(a.out)))
    try:
        if a.sample:
            files = [Path(str(pkg / "fixtures" / "openfda_sample.json"))]
            sources = ["SAMPLE FIXTURE (not real label text)"]
        elif a.labels:
            files = a.labels
            sources = [f"openFDA drug label files: {', '.join(p.name for p in files)}"]
        else:
            from .openfda import download_all
            status.write("downloading", "Contacting the FDA download server")

            def progress(i, n, name, size_mb):
                status.write("downloading", f"Downloading FDA label file {i} of {n} ({size_mb:.0f} MB)", step=i, steps=n)

            try:
                files = download_all(a.cache, progress)
            except Exception as e:
                raise RuntimeError(f"Couldn't download from the FDA ({type(e).__name__}: {e}). "
                                   "Check the internet connection, then run the drug database download again.") from e
            sources = [f"openFDA drug label bulk export ({len(files)} partitions; source: DailyMed SPL)"]

        status.write("building", "Building the drug database (removing duplicate labels, indexing)")
        stats = build(files, a.out, tox, a.cache / "work", sources)
        status.write("done", f"{stats['drugs']} medications ({stats['rx']} Rx, {stats['otc']} OTC) from {stats['seen']} FDA labels",
                     stats=dict(stats))
    except Exception as e:
        status.write("failed", str(e)[:500])
        logging.error("drug database build failed: %s", e)
        sys.exit(1)
    print(json.dumps(dict(stats), indent=2))
    print(f"wrote {a.out} ({a.out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
