"""Runtime configuration (environment variables)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    drugs_db: Path
    ehr_db: Path
    key_file: Path            # 32-byte EHR encryption key, hex. Put on removable USB for stronger protection.
    secret_file: Path         # session-cookie signing secret
    site_name: str
    secure_cookies: bool      # True when served over HTTPS
    session_hours: int
    allow_plaintext: bool     # DEV ONLY: run EHR without SQLCipher
    public_reference: bool = True  # drug/tox pages viewable without login (no patient data on them)
    tls_cert: Path | None = None   # managed by the trust center
    tls_key: Path | None = None
    backup_default_dest: str = ""  # suggested off-device backup path (USB / network mount)
    state_dir: Path | None = None
    idle_minutes: int = 30         # sign out after this long without activity
    grant_minutes: int = 30        # how long a justified chart access lasts
    kiosk_dir: Path | None = None  # token + pinned certificate shared with the touchscreen (no patient data)

    def __post_init__(self):
        sd = self.secret_file.parent
        self.tls_cert = Path(self.tls_cert) if self.tls_cert else sd / "tls.crt"
        self.tls_key = Path(self.tls_key) if self.tls_key else sd / "tls.key"
        self.state_dir = Path(self.state_dir) if self.state_dir else self.ehr_db.parent
        self.kiosk_dir = Path(self.kiosk_dir) if self.kiosk_dir else self.state_dir / "kiosk"

    @property
    def secrets_dir(self) -> Path:
        return self.secret_file.parent

    @classmethod
    def from_env(cls) -> "Settings":
        state = Path(_env("DRP_STATE_DIR", "/var/lib/drprepper"))
        secrets_dir = Path(_env("DRP_SECRETS_DIR", "/etc/drprepper"))
        return cls(
            drugs_db=Path(_env("DRP_DRUGS_DB", str(state / "drugs.db"))),
            ehr_db=Path(_env("DRP_EHR_DB", str(state / "ehr.db"))),
            key_file=Path(_env("DRP_EHR_KEY_FILE", str(secrets_dir / "ehr.key"))),
            secret_file=Path(_env("DRP_SECRET_FILE", str(secrets_dir / "session.secret"))),
            site_name=_env("DRP_SITE_NAME", "Dr.Prepper Field EHR"),
            secure_cookies=_env("DRP_SECURE_COOKIES", "1") == "1",
            session_hours=int(_env("DRP_SESSION_HOURS", "12")),
            allow_plaintext=_env("DRP_ALLOW_PLAINTEXT_EHR", "0") == "1",
            public_reference=_env("DRP_PUBLIC_REFERENCE", "1") == "1",
            tls_cert=Path(_env("DRP_TLS_CERT", str(secrets_dir / "tls.crt"))),
            tls_key=Path(_env("DRP_TLS_KEY", str(secrets_dir / "tls.key"))),
            backup_default_dest=_env("DRP_BACKUP_DEST", ""),
            state_dir=state,
            idle_minutes=int(_env("DRP_IDLE_MINUTES", "30")),
            grant_minutes=int(_env("DRP_GRANT_MINUTES", "30")),
            kiosk_dir=Path(_env("DRP_KIOSK_DIR", str(state / "kiosk"))),
        )


def ensure_secret(path: Path, nbytes: int = 32) -> str:
    """Read a hex secret, creating it (mode 0600) if missing."""
    if path.exists():
        val = path.read_text().strip()
        if len(val) != nbytes * 2:
            raise ValueError(f"{path} must contain {nbytes * 2} hex characters")
        return val
    path.parent.mkdir(parents=True, exist_ok=True)
    val = secrets.token_hex(nbytes)
    write_private(path, val + "\n")
    return val


def write_private(path: Path, text: str | bytes) -> None:
    """Atomically write a file readable only by the service user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = text.encode() if isinstance(text, str) else text
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
