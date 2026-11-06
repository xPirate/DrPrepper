"""drprepper-manage — administer the field EHR.

  drprepper-manage init --last Rivera --first Sam [--credential EMT-P]   # schema + first admin (one-time password)
  drprepper-manage create-user hsmith --last Smith --first Harold --credential EMT-B --role provider
  drprepper-manage reset-password hsmith
  drprepper-manage list-users
  drprepper-manage backup /mnt/drp-backup            # passphrase-protected bundle (uses the stored passphrase, or asks)
  drprepper-manage verify-backup FILE.drpbak
  drprepper-manage restore FILE.drpbak               # e.g. on replacement hardware, before first use
  drprepper-manage verify-audit
  drprepper-manage serve --port 8443                 # TLS with the trust-center certificate
  drprepper-manage syslog-listen --port 6514 --tls   # test receiver that prints what it gets
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .auditlog import audit, seal, verify
from .auth import generate_password, hash_password
from .config import Settings
from .ehr import EHR, ROLES, display_name, now

SYSTEM_CLI = {"id": None, "full_name": "System (command line)"}


def _ehr() -> EHR:
    e = EHR(Settings.from_env())
    if e.locked:                      # sealed key: ask for the unlock passphrase on the terminal
        import getpass
        from .keyseal import SealError
        try:
            e.unlock(getpass.getpass("Unlock passphrase: "))
        except SealError as err:
            sys.exit(str(err))
    else:
        e.init_schema()
    return e


def _create(e: EHR, username: str, last: str, first: str, credential: str, role: str) -> None:
    pw = generate_password()
    full = display_name(last, first, credential)
    with e.session() as c:
        if c.execute("SELECT 1 FROM users WHERE username = ?", (username.lower(),)).fetchone():
            sys.exit(f"user {username} already exists")
        cur = c.execute("""INSERT INTO users(username, last_name, first_name, credential, full_name, role, pw_hash, must_change_pw, created_at)
                           VALUES (?,?,?,?,?,?,?,1,?)""", (username.lower(), last, first, credential, full, role, hash_password(pw), now()))
        audit(c, SYSTEM_CLI, "user_create", entity="user", entity_id=cur.lastrowid, target=full, detail=f"username {username}, role {role}")
    print(f"Created {role} {full} (username '{username}'). One-time password: {pw}")
    print("They will choose a new password at first login.")


def cmd_init(a) -> None:
    e = _ehr()
    with e.session() as c:
        if c.execute("SELECT 1 FROM users").fetchone():
            print("EHR already initialised (users exist). Nothing to do.")
            return
    _create(e, a.username, a.last, a.first, a.credential, "admin")
    print(f"EHR database: {e.s.ehr_db}  (encrypted: {e.encrypted})")
    if e.encrypted:
        print(f"Encryption key: {e.s.key_file}  <- included in passphrase-protected backups; set a backup passphrase in Admin > Backup.")


def cmd_create_user(a) -> None:
    _create(_ehr(), a.username, a.last, a.first, a.credential, a.role)


def cmd_reset(a) -> None:
    e = _ehr()
    pw = generate_password()
    with e.session() as c:
        row = c.execute("SELECT id, full_name FROM users WHERE username = ?", (a.username.lower(),)).fetchone()
        if not row:
            sys.exit(f"no such user: {a.username}")
        c.execute("UPDATE users SET pw_hash = ?, must_change_pw = 1, active = 1 WHERE id = ?", (hash_password(pw), row["id"]))
        audit(c, SYSTEM_CLI, "user_reset_pw", entity="user", entity_id=row["id"], target=row["full_name"])
    print(f"Password for '{a.username}' reset. One-time password: {pw}")


def cmd_list_users(a) -> None:
    e = _ehr()
    with e.session() as c:
        for u in c.execute("SELECT username, full_name, role, active FROM users ORDER BY role, last_name"):
            print(f"{u['username']:<16} {u['role']:<9} {'active' if u['active'] else 'DISABLED':<9} {u['full_name']}")


def _passphrase(prompt: str, stored: str = "", confirm: bool = False) -> str:
    env = os.environ.get("DRP_BACKUP_PASSPHRASE")
    if env:
        return env
    if stored:
        return stored
    p = getpass.getpass(prompt)
    if confirm and getpass.getpass("Again: ") != p:
        sys.exit("Passphrases don't match.")
    return p


def cmd_backup(a) -> None:
    from . import backup
    e = _ehr()
    with e.session() as c:
        cfg = backup.load_config(c, e.s.backup_default_dest)
    pw = _passphrase("Backup passphrase: ", cfg["passphrase"], confirm=not cfg["passphrase"])
    try:
        path = backup.create_bundle(e, Path(a.dest), "manual", pw, a.include_drugs or cfg.get("include_drugs", False))
    except backup.BackupError as err:
        sys.exit(str(err))
    with e.session() as c:
        audit(c, SYSTEM_CLI, "backup_ok", detail=f"manual (CLI), {path.name}, {path.stat().st_size / 1e6:.1f} MB")
    print(f"Backup written: {path}")
    warn = backup.dest_warning(str(Path(a.dest)), e.s.state_dir)
    if warn:
        print(f"Warning: {warn}")


def cmd_verify_backup(a) -> None:
    from . import backup
    e = _ehr()
    pw = _passphrase("Backup passphrase (Enter to check checksums only): ") if not a.no_decrypt else None
    try:
        r = backup.verify_bundle(e, Path(a.file), pw or None)
    except backup.BackupError as err:
        sys.exit(f"FAILED: {err}")
    m = r["manifest"]
    print(f"{r['file']}: checksums OK; made {m['created']} on {m['source_host']}; {m['counts']}")
    if r["decrypted"]:
        print(f"Decrypted and opened: integrity {r['integrity']}, {r['patients']} patients.")


def cmd_restore(a) -> None:
    from . import backup
    e = _ehr()
    print(f"This replaces ALL records in {e.s.ehr_db} with the backup {a.file}.")
    if not a.yes and input("Type RESTORE to continue: ").strip() != "RESTORE":
        sys.exit("Cancelled.")
    pw = _passphrase("Backup passphrase: ")
    try:
        r = backup.restore_bundle(e, Path(a.file), pw, restore_tls=not a.keep_cert, actor="command line")
    except backup.BackupError as err:
        sys.exit(f"Restore failed, nothing was changed: {err}")
    print(f"Restored backup from {r['manifest']['created']} ({r['manifest']['source_host']}).")
    print(f"Previous data kept in {r['kept']}. Certificate restored: {r['tls_restored']}.")
    print("Restart the web service:  sudo systemctl restart drprepper-web")


def cmd_verify_audit(a) -> None:
    e = _ehr()
    seal(e)
    with e.session() as c:
        r = verify(c)
        audit(c, SYSTEM_CLI, "audit_verify", detail=("passed" if r["ok"] else f"FAILED at entry {r['broken_id']}") + f", {r['checked']} entries")
    print(r["message"])
    sys.exit(0 if r["ok"] else 1)


class _NoQuery:
    """Access-log filter: drop query strings so patient searches and display keys never reach the system log."""

    def filter(self, record) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            args = list(record.args)
            args[2] = args[2].split("?", 1)[0] + ("?…" if "?" in args[2] else "")
            record.args = tuple(args)
        return True


def access_log_config() -> dict:
    from uvicorn.config import LOGGING_CONFIG
    import copy
    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["noquery"] = {"()": "drprepper.server.manage._NoQuery"}
    cfg["handlers"]["access"]["filters"] = ["noquery"]
    return cfg


def cmd_seal_key(a) -> None:
    import getpass
    from .keyseal import MIN_PASSPHRASE, sealed_path
    s = Settings.from_env()
    if sealed_path(s).exists():
        sys.exit("The key is already sealed.")
    e = EHR(s)
    p1 = getpass.getpass(f"New unlock passphrase ({MIN_PASSPHRASE}+ characters): ")
    if len(p1) < MIN_PASSPHRASE or p1 != getpass.getpass("Again: "):
        sys.exit("Too short, or the two didn't match. Nothing changed.")
    e.seal_key(p1)
    print(f"Sealed. {s.key_file} was removed; restart the service and unlock at https://<unit>:8443/unlock.")


def cmd_unseal_key(a) -> None:
    import getpass
    from .keyseal import SealError, sealed_path, unseal
    s = Settings.from_env()
    if not sealed_path(s).exists():
        sys.exit("The key isn't sealed.")
    try:
        key, _, _ = unseal(sealed_path(s).read_bytes(), getpass.getpass("Current unlock passphrase: "))
    except SealError as err:
        sys.exit(str(err))
    from .config import write_private
    from .keyseal import shred
    write_private(s.key_file, key + "\n")
    shred(sealed_path(s))
    print(f"Key stored in {s.key_file}. The unit will start unattended.")


def cmd_serve(a) -> None:
    import uvicorn
    from . import trust
    s = Settings.from_env()
    kw = {}
    if not a.no_tls:
        if trust.ensure_default(s.tls_cert, s.tls_key):
            print(f"Created a self-signed certificate at {s.tls_cert}. Manage it in Admin > Trust center.")
        kw.update(ssl_certfile=str(s.tls_cert), ssl_keyfile=str(s.tls_key))
    config = uvicorn.Config("drprepper.server.app:app_factory", factory=True, host=a.host, port=a.port,
                            proxy_headers=False, workers=1, log_level="info", server_header=False,
                            log_config=access_log_config(), **kw)
    config.load()
    if config.ssl:
        import ssl as _ssl
        config.ssl.minimum_version = _ssl.TLSVersion.TLSv1_2      # explicit, not left to library defaults
        config.ssl.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!MD5:!DSS")
    trust.register_live_context(config.ssl)   # lets the trust center swap certificates without a restart
    uvicorn.Server(config).run()


def cmd_syslog_listen(a) -> None:
    """Minimal receiver for testing forwarding (prints each message)."""
    import socket
    import socketserver
    import ssl
    import threading

    def show(data: bytes, peer):
        print(f"[{peer[0]}] {data.decode('utf-8', 'replace').strip()}", flush=True)

    class TCPHandler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                self._read()
            except (ConnectionError, ssl.SSLError, ValueError):
                return   # sender closed the connection; normal between batches

        def _read(self):
            f = self.rfile
            while True:
                head = b""
                while (ch := f.read(1)) and ch not in (b" ", b"\n"):
                    head += ch
                if not ch:
                    return
                if ch == b" " and head.isdigit():          # octet counting
                    show(f.read(int(head)), self.client_address)
                else:                                        # newline framing
                    rest = b"" if ch == b"\n" else f.readline()
                    show(head + (b" " + rest if ch == b" " else b""), self.client_address)

    class UDPHandler(socketserver.BaseRequestHandler):
        def handle(self):
            show(self.request[0], self.client_address)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    tcp = socketserver.ThreadingTCPServer((a.host, a.port), TCPHandler)
    if a.tls:
        from . import trust
        cert, key = Path(a.cert or "/tmp/syslog-test.crt"), Path(a.key or "/tmp/syslog-test.key")
        if not cert.exists():
            c, k = trust.generate_self_signed("syslog-test", "syslog-test\nlocalhost\n127.0.0.1", 365)
            cert.write_bytes(c); key.write_bytes(k)
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(str(cert), str(key))
        tcp.socket = ctx.wrap_socket(tcp.socket, server_side=True)
        print(f"TLS certificate for the receiver: {cert} (paste it as the CA in Syslog settings, or untick the certificate check)")
    udp = socketserver.ThreadingUDPServer((a.host, a.port if not a.tls else a.udp_port), UDPHandler)
    threading.Thread(target=udp.serve_forever, daemon=True).start()
    print(f"Listening: {'TLS' if a.tls else 'TCP'} {a.host}:{a.port}, UDP {a.host}:{a.port if not a.tls else a.udp_port}", flush=True)
    tcp.serve_forever()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="drprepper-manage", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def person(p, username_default=None):
        if username_default is None:
            p.add_argument("username")
        else:
            p.add_argument("--username", default=username_default)
        p.add_argument("--last", required=True, help="last name")
        p.add_argument("--first", required=True, help="first name")
        p.add_argument("--credential", default="", help="e.g. EMT-B, EMT-P, RN, MD")

    p = sub.add_parser("init", help="create schema and first admin")
    person(p, "admin")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("create-user")
    person(p)
    p.add_argument("--role", choices=ROLES, default="provider")
    p.set_defaults(fn=cmd_create_user)

    p = sub.add_parser("reset-password"); p.add_argument("username"); p.set_defaults(fn=cmd_reset)
    p = sub.add_parser("list-users"); p.set_defaults(fn=cmd_list_users)

    p = sub.add_parser("backup", help="write a passphrase-protected backup bundle")
    p.add_argument("dest", help="directory (USB drive / network share)")
    p.add_argument("--include-drugs", action="store_true")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("verify-backup"); p.add_argument("file"); p.add_argument("--no-decrypt", action="store_true")
    p.set_defaults(fn=cmd_verify_backup)

    p = sub.add_parser("restore", help="replace the EHR with a backup bundle")
    p.add_argument("file"); p.add_argument("--yes", action="store_true", help="don't ask to type RESTORE")
    p.add_argument("--keep-cert", action="store_true", help="keep this unit's current certificate")
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("verify-audit"); p.set_defaults(fn=cmd_verify_audit)

    p = sub.add_parser("seal-key", help="require an unlock passphrase at every start (key no longer stored in plain)")
    p.set_defaults(fn=cmd_seal_key)
    p = sub.add_parser("unseal-key", help="store the key as a plain file again (unattended start)")
    p.set_defaults(fn=cmd_unseal_key)

    p = sub.add_parser("serve")
    p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=8443)
    p.add_argument("--no-tls", action="store_true", help="plain HTTP (development only)")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("syslog-listen", help="test receiver")
    p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=514)
    p.add_argument("--tls", action="store_true"); p.add_argument("--udp-port", type=int, default=514)
    p.add_argument("--cert"); p.add_argument("--key")
    p.set_defaults(fn=cmd_syslog_listen)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
