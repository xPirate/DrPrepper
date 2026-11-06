"""Download and stream openFDA drug label bulk files.

openFDA publishes every SPL drug label (sourced from DailyMed) as zipped JSON
partitions listed at https://api.fda.gov/download.json. Each partition holds
{"meta": ..., "results": [label, ...]}. Files are large, so we stream records.
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

DOWNLOAD_INDEX = "https://api.fda.gov/download.json"


def list_partitions(session) -> list[dict]:
    r = session.get(DOWNLOAD_INDEX, timeout=60)
    r.raise_for_status()
    label = r.json()["results"]["drug"]["label"]
    log.info("openFDA label export: %s records, %d partitions, exported %s",
             label.get("total_records"), len(label["partitions"]), label.get("export_date"))
    return label["partitions"]


def download_all(cache: Path, progress=None) -> list[Path]:
    """Download (or reuse cached) label partitions. Returns local zip paths.
    `progress(i, n, name, size_mb)` is called before each partition."""
    import requests

    cache.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = "drprepper-ingest/0.1 (offline medical reference builder)"
    paths: list[Path] = []
    parts = list_partitions(s)
    for i, part in enumerate(parts, 1):
        url = part["file"]
        dest = cache / url.rsplit("/", 1)[-1]
        if progress:
            progress(i, len(parts), dest.name, float(part.get("size_mb", 0) or 0))
        if dest.exists() and zipfile.is_zipfile(dest):
            log.info("cached  %s", dest.name)
        else:
            log.info("fetch   %s (%.0f MB)", dest.name, float(part.get("size_mb", 0)))
            tmp = dest.with_suffix(".part")
            with s.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            if not zipfile.is_zipfile(tmp):
                raise RuntimeError(f"downloaded file is not a zip: {url}")
            tmp.rename(dest)
        paths.append(dest)
    return paths


def iter_labels(path: Path) -> Iterator[dict]:
    """Yield label records from a .json.zip partition or a plain .json file."""
    import ijson

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith(".json"):
                    with zf.open(name) as raw:
                        yield from ijson.items(io.BufferedReader(raw), "results.item", use_float=True)
    else:
        with open(path, "rb") as f:
            # Small fixtures: plain json.load keeps it simple.
            data = json.load(f)
            yield from data.get("results", [])
