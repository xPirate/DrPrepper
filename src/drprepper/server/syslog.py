"""Forward sealed audit entries to a syslog server (RFC 5424 over UDP, TCP or TLS).

A cursor (last forwarded audit id) is kept in the settings table, so entries
written while the server is unreachable are sent when it comes back. UDP has no
delivery confirmation; use TCP or TLS where losing an entry matters.
"""
from __future__ import annotations

import logging
import socket
import ssl
import threading
import time
from datetime import datetime

from .ehr import get_setting, now, set_setting

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False, "host": "", "port": 6514, "transport": "tls", "framing": "octet",
    "verify": True, "ca_pem": "", "facility": 20, "hostname": "", "app_name": "drprepper",
}
SEVERITY = {"warning": 4, "notice": 5, "info": 6}
FACILITIES = {16: "local0", 17: "local1", 18: "local2", 19: "local3", 20: "local4", 21: "local5", 22: "local6", 23: "local7",
              10: "authpriv", 13: "log audit"}
SD_ID = "drp@32473"   # 32473 = IANA example enterprise number; replace with your own PEN if you have one


def load_config(c) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(get_setting(c, "syslog", {}) or {})
    return cfg


def _sd_escape(v) -> str:
    return str(v if v is not None else "").replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def format_5424(r: dict, cfg: dict) -> bytes:
    pri = int(cfg["facility"]) * 8 + SEVERITY.get(r.get("severity") or "info", 6)
    ts = datetime.fromisoformat(r["at"]).isoformat(timespec="seconds")
    host = (cfg.get("hostname") or socket.gethostname() or "-")[:255].replace(" ", "_")
    app = (cfg.get("app_name") or "drprepper")[:48]
    msgid = (r.get("action") or "-")[:32]
    sd = (f'[{SD_ID} id="{r.get("id", 0)}" user="{_sd_escape(r.get("user_display"))}" action="{_sd_escape(r.get("action"))}"'
          f' pnum="{_sd_escape(r.get("pnum"))}" client="{_sd_escape(r.get("client"))}" hash="{_sd_escape(r.get("hash"))}"]')
    return f"<{pri}>1 {ts} {host} {app} - {msgid} {sd} {redact(r)}".encode("utf-8", "replace")


def redact(r: dict) -> str:
    """Keep free text people typed (search terms) out of the copy that leaves the unit; the local audit keeps it."""
    msg = r.get("message") or ""
    if r.get("action") == "patient_search":
        import re
        msg = re.sub(r"\('.*?', (\d+ result)", r"(search text withheld, \1", msg)
    return msg


class Sender:
    def __init__(self, cfg: dict, timeout: float = 8.0):
        self.cfg, self.timeout, self.sock = cfg, timeout, None

    def _ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        if self.cfg.get("ca_pem"):
            ctx.load_verify_locations(cadata=self.cfg["ca_pem"])
        if not self.cfg.get("verify", True):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def open(self):
        host, port, t = self.cfg["host"], int(self.cfg["port"]), self.cfg["transport"]
        if not host:
            raise ValueError("No syslog server configured")
        if t == "udp":
            info = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
            self.sock = socket.socket(info[0], socket.SOCK_DGRAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(info[4])
        else:
            raw = socket.create_connection((host, port), timeout=self.timeout)
            self.sock = self._ctx().wrap_socket(raw, server_hostname=host) if t == "tls" else raw
        return self

    def send(self, msg: bytes) -> None:
        if self.cfg["transport"] == "udp":
            self.sock.send(msg[:8192])
        elif self.cfg.get("framing", "octet") == "octet":
            self.sock.sendall(str(len(msg)).encode() + b" " + msg)
        else:
            self.sock.sendall(msg.replace(b"\n", b" ") + b"\n")

    def close(self):
        """Close gracefully. An abrupt close with unread bytes (e.g. TLS 1.3 session tickets) makes the
        kernel send a reset, and the receiver can drop messages it hasn't read yet."""
        if not self.sock:
            return
        s, self.sock = self.sock, None
        try:
            if self.cfg["transport"] != "udp":
                s.settimeout(3)
                if isinstance(s, ssl.SSLSocket):
                    try:
                        s = s.unwrap()          # send close_notify
                    except (ssl.SSLError, OSError):
                        pass
                try:
                    s.shutdown(socket.SHUT_WR)
                    while s.recv(4096):         # drain until the server closes its side
                        pass
                except OSError:
                    pass
        finally:
            s.close()


def send_test(cfg: dict, who: str) -> None:
    r = {"id": 0, "at": now(), "user_display": who, "action": "syslog_test", "severity": "info", "pnum": "", "client": "",
         "hash": "", "message": f"{datetime.now():%Y-%m-%d %H:%M:%S} - {who} sent a Dr.Prepper syslog test message"}
    s = Sender(cfg).open()
    try:
        s.send(format_5424(r, cfg))
    finally:
        s.close()


def pump(ehr, batch: int = 200) -> int:
    """Send pending sealed entries once. Returns how many were sent. Raises on failure."""
    c = ehr.connect()
    try:
        cfg = load_config(c)
        if not cfg["enabled"] or not cfg["host"]:
            return 0
        cursor = get_setting(c, "syslog_cursor", 0)
        rows = c.execute("SELECT * FROM audit WHERE id > ? AND hash IS NOT NULL ORDER BY id LIMIT ?", (cursor, batch)).fetchall()
        if not rows:
            return 0
        s = Sender(cfg).open()
        try:
            for r in rows:
                s.send(format_5424(r, cfg))
                cursor = r["id"]
        finally:
            s.close()
            set_setting(c, "syslog_cursor", cursor)   # keep progress even if a later send failed
            c.commit()
        set_setting(c, "syslog_status", {"last_ok": now(), "last_error": "", "last_id": cursor})
        c.commit()
        return len(rows)
    finally:
        c.close()


def status(c) -> dict:
    st = get_setting(c, "syslog_status", {}) or {}
    cursor = get_setting(c, "syslog_cursor", 0)
    st["pending"] = c.execute("SELECT COUNT(*) AS n FROM audit WHERE id > ? AND hash IS NOT NULL", (cursor,)).fetchone()["n"]
    st["cursor"] = cursor
    return st


class Forwarder(threading.Thread):
    def __init__(self, ehr):
        super().__init__(name="syslog-forwarder", daemon=True)
        self.ehr, self.stop_event, self.backoff = ehr, threading.Event(), 2.0

    def run(self):
        while not self.stop_event.wait(self.backoff):
            if self.ehr.maintenance.is_set():
                continue
            try:
                sent = pump(self.ehr)
                self.backoff = 0.5 if sent else 2.0
            except Exception as e:  # network down, bad cert, server gone: keep the cursor and retry
                log.warning("syslog forward failed: %s", e)
                try:
                    c = self.ehr.connect()
                    st = get_setting(c, "syslog_status", {}) or {}
                    st.update(last_error=f"{now()[:19]}: {e}")
                    set_setting(c, "syslog_status", st)
                    c.commit()
                    c.close()
                except Exception:
                    pass
                self.backoff = min(self.backoff * 2, 60.0)
