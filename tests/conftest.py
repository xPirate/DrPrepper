import os
os.environ.setdefault("DRP_PBKDF2_ITERATIONS", "20000")   # fast hashing in tests; production default is 600,000
import re
from pathlib import Path

import pytest

from drprepper.ingest.__main__ import main as ingest_main

PW = "correct horse battery"


@pytest.fixture(scope="session")
def drugs_db(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("drugs") / "drugs.db"
    ingest_main(["--out", str(out), "--sample", "--cache", str(out.parent / "cache")])
    return out


@pytest.fixture()
def settings(tmp_path, drugs_db):
    from drprepper.server.config import Settings
    (tmp_path / "state").mkdir()
    return Settings(
        drugs_db=drugs_db, ehr_db=tmp_path / "state" / "ehr.db", key_file=tmp_path / "secrets" / "ehr.key",
        secret_file=tmp_path / "secrets" / "session.secret", site_name="Test EHR", secure_cookies=False,
        session_hours=1, allow_plaintext=False, public_reference=True, state_dir=tmp_path / "state",
    )


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" (?:value|content)="([^"]+)"', html)
    assert m, "no csrf token on page"
    return m.group(1)


class Browser:
    """TestClient wrapper that carries the CSRF token like a real form post would."""

    def __init__(self, client):
        self.c = client
        self.csrf = None

    def get(self, url, **kw):
        r = self.c.get(url, **kw)
        if "text/html" in r.headers.get("content-type", "") and 'name="csrf"' in r.text:
            self.csrf = csrf_of(r.text)
        return r

    def post(self, url, data=None, **kw):
        if self.csrf is None:
            self.get("/login")
        return self.c.post(url, data={"csrf": self.csrf, **(data or {})}, **kw)

    def api(self, url):
        return self.c.post(url, headers={"X-CSRF-Token": self.csrf or ""})


def add_user(app, username, role, last, first, cred="", pw=PW):
    from drprepper.server.auth import hash_password
    from drprepper.server.ehr import display_name, now
    with app.state.ehr.session() as c:
        cur = c.execute("""INSERT INTO users(username, last_name, first_name, credential, full_name, role, pw_hash, must_change_pw, created_at)
                           VALUES (?,?,?,?,?,?,?,0,?)""", (username, last, first, cred, display_name(last, first, cred), role,
                                                           hash_password(pw), now()))
        return cur.lastrowid


@pytest.fixture()
def app(settings):
    from drprepper.server.app import create_app
    a = create_app(settings, start_workers=False)
    from drprepper.server.ehr import seed_locations
    with a.state.ehr.session() as c:
        seed_locations(c)              # fresh installs start with no locations; tests use the sample layout
    a.state.uids = {
        "admin": add_user(a, "admin", "admin", "Rivera", "Sam"),
        "triage": add_user(a, "triage", "triage", "Ortega", "Maria", "RN"),
        "smith": add_user(a, "smith", "provider", "Smith", "Harold", "EMT-B"),
        "williams": add_user(a, "williams", "provider", "Williams", "Abigail", "MD"),
        "viewer": add_user(a, "viewer", "viewer", "Park", "Dana"),
    }
    return a


@pytest.fixture()
def new_browser(app):
    from fastapi.testclient import TestClient

    def make(user=None):
        b = Browser(TestClient(app, follow_redirects=False))
        if user:
            login(b, user)
        return b
    return make


def login(b: Browser, user="admin", pw=PW):
    b.get("/login")
    r = b.post("/login", {"username": user, "password": pw, "next": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/", r.text
    b.get("/")
    return b


def pid_from(r) -> int:
    return int(r.headers["location"].rstrip("/").rsplit("/", 1)[1])


def audit_messages(app) -> list[str]:
    with app.state.ehr.session() as c:
        return [r["message"] for r in c.execute("SELECT message FROM audit ORDER BY id")]
