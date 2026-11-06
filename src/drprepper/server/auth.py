"""Password hashing, login throttling and CSRF helpers (stdlib only)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict

# OWASP (2023) guidance for PBKDF2-HMAC-SHA256. Older hashes are upgraded at the next successful login.
ITERATIONS = int(os.environ.get("DRP_PBKDF2_ITERATIONS", "600000"))
MIN_PASSWORD = 10
# Passwords people pick first. A short list is enough to stop the obvious ones (NIST SP 800-63B blocklist).
COMMON = {
    "password", "password1", "password12", "password123", "password1234", "passw0rd", "p@ssword", "p@ssw0rd",
    "1234567890", "12345678910", "0123456789", "qwertyuiop", "qwerty1234", "qwerty12345", "1q2w3e4r5t",
    "iloveyou12", "letmein123", "welcome123", "welcome1234", "changeme123", "admin12345", "administrator",
    "abc1234567", "abcdefghij", "trustno1234", "football12", "baseball12", "superman12", "monkey1234",
    "sunshine12", "princess12", "dragon1234", "master1234", "starwars12", "drprepper1", "drprepper123",
    "fieldhospital", "paramedic1", "paramedic123", "emergency1", "emergency123", "hospital123", "nurse12345",
}


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


_DUMMY = None


def dummy_verify(pw: str) -> None:
    """Spend the same time as a real check, so unknown usernames can't be told apart by timing."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_urlsafe(16))
    verify_password(pw, _DUMMY)


def needs_rehash(stored: str) -> bool:
    try:
        return int(stored.split("$")[1]) < ITERATIONS
    except (IndexError, ValueError):
        return True


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def password_problem(pw: str, username: str = "") -> str | None:
    if len(pw) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    low = pw.lower()
    if low in COMMON or len(set(low)) <= 2:
        return "That password is too common or too easy to guess. Try a short phrase of unrelated words."
    if username and username.lower() in low:
        return "Don't use your username in your password."
    return None


def generate_password() -> str:
    words = secrets.token_urlsafe(12)
    return words.replace("_", "x").replace("-", "y")


class Throttle:
    """Slow down and lock repeated failures (in-memory).

    Keys are a username+client pair and a client alone, so someone guessing from one device can't keep a
    provider locked out from every other device. A client hammering many usernames is still stopped.
    """

    def __init__(self, max_failures: int = 5, window_s: int = 300, client_max: int = 20):
        self.max = max_failures
        self.client_max = client_max
        self.window = window_s
        self.fail: dict[str, list[float]] = defaultdict(list)

    def limit(self, key: str) -> int:
        return self.client_max if key.startswith("c:") else self.max

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.window
        self.fail[key] = [t for t in self.fail[key] if t > cutoff]
        return self.fail[key]

    def locked(self, *keys: str) -> bool:
        return any(len(self._recent(k)) >= self.limit(k) for k in keys)

    def delay(self, *keys: str) -> float:
        """Growing pause after each failure: 0.5 s, 1 s, 2 s ... capped at 4 s."""
        n = max((len(self._recent(k)) for k in keys), default=0)
        return min(4.0, 0.5 * (2 ** max(0, n - 1))) if n else 0.0

    def failure(self, *keys: str) -> None:
        for k in keys:
            self.fail[k].append(time.monotonic())

    def success(self, *keys: str) -> None:
        for k in keys:
            self.fail.pop(k, None)


def new_csrf() -> str:
    return secrets.token_urlsafe(24)
