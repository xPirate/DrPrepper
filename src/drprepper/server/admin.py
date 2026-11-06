"""Admin routes: users, audit trail, syslog, trust center, backup and restore."""
from __future__ import annotations

import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import auditlog, backup, syslog, trust
from .auditlog import VERBS, audit
from .auth import generate_password, hash_password
from .ehr import CREDENTIALS, ROLES, display_name, get_setting, now, set_setting


def register(app, ctx) -> None:
    s, ehr, render, flash, form = ctx.s, ctx.ehr, ctx.render, ctx.flash, ctx.form

    def _install_and_publish(*a):
        from .intake import publish_cert
        info = trust.install(*a)
        publish_cert(s)          # the touchscreen pins the certificate; give it the new one
        return info

    def admin(req: Request) -> dict:
        return ctx.current_user(req, admin=True)

    # ------------------------------------------------------------------ users
    def user_values(f: dict) -> dict:
        v = {"last_name": f.get("last_name", ""), "first_name": f.get("first_name", ""),
             "credential": f.get("credential_other") if f.get("credential") == "Other" else f.get("credential", ""),
             "role": f.get("role", "")}
        if not v["last_name"] or not v["first_name"]:
            raise ValueError("Last and first name are required.")
        if v["role"] not in ROLES:
            raise ValueError("Choose a role.")
        v["full_name"] = display_name(v["last_name"], v["first_name"], v["credential"])
        return v

    @app.get("/admin/users", response_class=HTMLResponse)
    async def users_view(req: Request):
        user = admin(req)
        live_since = datetime.fromtimestamp(datetime.now().timestamp() - s.idle_minutes * 60).astimezone().isoformat(timespec="seconds")
        with ehr.session() as c:
            rows = c.execute("""SELECT u.*, (SELECT COUNT(*) FROM patients p WHERE p.assigned_to = u.id) AS patients,
                                       (SELECT COUNT(*) FROM sessions x WHERE x.user_id = u.id AND x.revoked_at IS NULL
                                          AND x.last_seen_at >= ?) AS sessions
                                FROM users u ORDER BY active DESC, last_name, first_name""", (live_since,)).fetchall()
        # Accounts nobody has used for 30 days are flagged for review (access review, ISO 27001 A.5.18).
        cutoff = datetime.now().astimezone().timestamp() - 30 * 86400
        stale = {r["id"] for r in rows if r["active"] and
                 datetime.fromisoformat(r["last_login_at"] or r["created_at"]).timestamp() < cutoff}
        return render(req, "users.html", user, rows=rows, stale=stale, temp_pw=req.session.pop("temp_pw", None),
                      roles=ROLES, credentials=CREDENTIALS)

    @app.post("/admin/users")
    async def users_create(req: Request):
        user = admin(req)
        f = await form(req)
        username = (f.get("username") or "").lower()
        try:
            if not username or not username.replace(".", "").replace("-", "").replace("_", "").isalnum():
                raise ValueError("Username: letters, numbers, dots, dashes or underscores.")
            v = user_values(f)
        except ValueError as e:
            flash(req, str(e), "error")
            return RedirectResponse("/admin/users", 303)
        pw = generate_password()
        h = await run_in_threadpool(hash_password, pw)
        with ehr.session() as c:
            if c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                flash(req, f"Username {username} already exists.", "error")
                return RedirectResponse("/admin/users", 303)
            cur = c.execute("""INSERT INTO users(username, last_name, first_name, credential, full_name, role, pw_hash,
                                                 must_change_pw, created_at) VALUES (?,?,?,?,?,?,?,1,?)""",
                            (username, v["last_name"], v["first_name"], v["credential"], v["full_name"], v["role"], h, now()))
            audit(c, user, "user_create", entity="user", entity_id=cur.lastrowid, target=v["full_name"],
                  detail=f"username {username}, role {v['role']}", client=ctx.client(req))
        req.session["temp_pw"] = [username, pw]
        return RedirectResponse("/admin/users", 303)

    @app.get("/admin/users/{uid}", response_class=HTMLResponse)
    async def user_edit_form(req: Request, uid: int):
        user = admin(req)
        with ehr.session() as c:
            u = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if not u:
                raise HTTPException(404, "User not found")
            pats = c.execute("SELECT COUNT(*) AS n FROM patients WHERE assigned_to = ?", (uid,)).fetchone()["n"]
        return render(req, "user_edit.html", user, u=u, roles=ROLES, credentials=CREDENTIALS, patients=pats)

    @app.post("/admin/users/{uid}")
    async def user_edit(req: Request, uid: int):
        user = admin(req)
        f = await form(req)
        try:
            v = user_values(f)
        except ValueError as e:
            flash(req, str(e), "error")
            return RedirectResponse(f"/admin/users/{uid}", 303)
        with ehr.session() as c:
            old = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if not old:
                raise HTTPException(404, "User not found")
            if uid == user["id"] and v["role"] != "admin":
                flash(req, "You can't remove your own admin role. Ask another admin.", "error")
                return RedirectResponse(f"/admin/users/{uid}", 303)
            changes = [f"{k} '{old[k]}' -> '{v[k]}'" for k in ("last_name", "first_name", "credential", "role") if (old[k] or "") != v[k]]
            c.execute("UPDATE users SET last_name=?, first_name=?, credential=?, full_name=?, role=? WHERE id = ?",
                      (v["last_name"], v["first_name"], v["credential"], v["full_name"], v["role"], uid))
            if old["role"] != v["role"]:
                ctx.revoke_user_sessions(c, uid, "role changed")
            audit(c, user, "user_update", entity="user", entity_id=uid, target=v["full_name"],
                  detail="; ".join(changes) or "no changes", client=ctx.client(req))
        flash(req, f"Saved {v['full_name']}.")
        return RedirectResponse("/admin/users", 303)

    @app.post("/admin/users/{uid}/reset")
    async def users_reset(req: Request, uid: int):
        user = admin(req)
        await form(req)
        pw = generate_password()
        h = await run_in_threadpool(hash_password, pw)
        with ehr.session() as c:
            u = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if not u:
                raise HTTPException(404, "User not found")
            c.execute("UPDATE users SET pw_hash = ?, must_change_pw = 1 WHERE id = ?", (h, uid))
            ctx.revoke_user_sessions(c, uid, "password reset by admin")
            audit(c, user, "user_reset_pw", entity="user", entity_id=uid, target=u["full_name"], client=ctx.client(req))
        req.session["temp_pw"] = [u["username"], pw]
        return RedirectResponse("/admin/users", 303)

    @app.post("/admin/users/{uid}/toggle")
    async def users_toggle(req: Request, uid: int):
        user = admin(req)
        await form(req)
        if uid == user["id"]:
            flash(req, "You can't disable your own account.", "error")
            return RedirectResponse("/admin/users", 303)
        with ehr.session() as c:
            u = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if not u:
                raise HTTPException(404, "User not found")
            c.execute("UPDATE users SET active = 1 - active WHERE id = ?", (uid,))
            if u["active"]:
                ctx.revoke_user_sessions(c, uid, "account disabled")
            audit(c, user, "user_disable" if u["active"] else "user_enable", entity="user", entity_id=uid,
                  target=u["full_name"], client=ctx.client(req))
            n = c.execute("SELECT COUNT(*) AS n FROM patients WHERE assigned_to = ?", (uid,)).fetchone()["n"]
        if u["active"] and n:
            flash(req, f"{u['full_name']} still has {n} assigned patient(s). Reassign them from the triage board.", "warn")
        return RedirectResponse("/admin/users", 303)

    @app.post("/admin/users/{uid}/signout")
    async def users_signout(req: Request, uid: int):
        user = admin(req)
        await form(req)
        with ehr.session() as c:
            u = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if not u:
                raise HTTPException(404, "User not found")
            keep = req.session.get("sid") if uid == user["id"] else None
            n = ctx.revoke_user_sessions(c, uid, "signed out by admin", keep_token=keep)
            audit(c, user, "user_signout", entity="user", entity_id=uid, target=u["full_name"],
                  detail=f"{n} session(s)", client=ctx.client(req))
        flash(req, f"Signed {u['full_name']} out of {n} session(s).")
        return RedirectResponse("/admin/users", 303)

    # ------------------------------------------------------------------ audit
    def audit_filters(q) -> dict:
        return {k: (q.get(k) or "").strip() for k in ("user_id", "pnum", "action", "severity", "date_from", "date_to", "text")}

    @app.get("/admin/audit", response_class=HTMLResponse)
    async def audit_view(req: Request, page: int = 1):
        user = admin(req)
        flt = audit_filters(req.query_params)
        per = 200
        with ehr.session() as c:
            rows, total = auditlog.query(c, **{k: (int(v) if k == "user_id" and v else v) or None for k, v in flt.items()},
                                         limit=per, offset=(max(page, 1) - 1) * per)
            people = c.execute("SELECT id, full_name FROM users ORDER BY last_name, first_name").fetchall()
            rv = auditlog.review(c)
        qs = "&".join(f"{k}={v}" for k, v in flt.items() if v)
        return render(req, "audit.html", user, rows=rows, total=total, page=page, per=per, flt=flt, people=people,
                      actions=sorted(VERBS), qs=qs, verify=req.session.pop("verify", None), review=rv)

    @app.get("/admin/audit/export.csv")
    async def audit_export(req: Request):
        user = admin(req)
        flt = audit_filters(req.query_params)
        with ehr.session() as c:
            rows, total = auditlog.query(c, **{k: (int(v) if k == "user_id" and v else v) or None for k, v in flt.items()},
                                         limit=1_000_000)
            desc = ", ".join(f"{k}={v}" for k, v in flt.items() if v) or "all entries"
            audit(c, user, "audit_export", detail=f"{total} rows, {desc}", client=ctx.client(req))
        name = f"drprepper-audit-{datetime.now():%Y%m%d-%H%M}.csv"
        return Response(auditlog.to_csv(rows), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/admin/audit/verify")
    async def audit_verify(req: Request):
        user = admin(req)
        await form(req)
        auditlog.seal(ehr)
        with ehr.session() as c:
            res = auditlog.verify(c)
            audit(c, user, "audit_verify", detail=("passed" if res["ok"] else f"FAILED at entry {res['broken_id']}")
                  + f", {res['checked']} entries", client=ctx.client(req))
        req.session["verify"] = res
        return RedirectResponse("/admin/audit", 303)

    # ------------------------------------------------------------------ syslog
    @app.get("/admin/syslog", response_class=HTMLResponse)
    async def syslog_view(req: Request):
        user = admin(req)
        with ehr.session() as c:
            cfg, st = syslog.load_config(c), syslog.status(c)
        return render(req, "syslog.html", user, cfg=cfg, st=st, facilities=syslog.FACILITIES)

    def syslog_from_form(f: dict) -> dict:
        try:
            port = int(f.get("port") or 0)
        except ValueError:
            port = 0
        if f.get("enabled") == "1" and not f.get("host"):
            raise ValueError("Enter the syslog server's address.")
        if not 1 <= port <= 65535:
            raise ValueError("Port must be 1-65535.")
        if f.get("transport") not in ("udp", "tcp", "tls"):
            raise ValueError("Choose UDP, TCP or TLS.")
        cfg = {"enabled": f.get("enabled") == "1", "host": f.get("host", ""), "port": port, "transport": f["transport"],
               "framing": "octet" if f.get("framing") != "newline" else "newline", "verify": f.get("verify") == "1",
               "ca_pem": f.get("ca_pem", ""), "facility": int(f.get("facility") or 20),
               "hostname": f.get("hostname", ""), "app_name": f.get("app_name") or "drprepper"}
        if cfg["ca_pem"] and "BEGIN CERTIFICATE" not in cfg["ca_pem"]:
            raise ValueError("The CA certificate must be in PEM format (-----BEGIN CERTIFICATE-----).")
        return cfg

    @app.post("/admin/syslog")
    async def syslog_save(req: Request):
        user = admin(req)
        f = await form(req)
        try:
            cfg = syslog_from_form(f)
        except ValueError as e:
            flash(req, str(e), "error")
            return RedirectResponse("/admin/syslog", 303)
        with ehr.session() as c:
            old = syslog.load_config(c)
            set_setting(c, "syslog", cfg)
            if f.get("start_from") == "now":
                last = c.execute("SELECT MAX(id) AS m FROM audit").fetchone()["m"] or 0
                set_setting(c, "syslog_cursor", last)
            audit(c, user, "syslog_settings", detail=f"{'on' if cfg['enabled'] else 'off'}, {cfg['transport'].upper()} "
                  f"{cfg['host']}:{cfg['port']}" + ("" if cfg["verify"] or cfg["transport"] != "tls" else ", certificate check OFF")
                  + (f" (was {old['transport'].upper()} {old['host']}:{old['port']})" if old["host"] and old["host"] != cfg["host"] else ""),
                  client=ctx.client(req))
        flash(req, "Syslog settings saved." + (" Forwarding is on." if cfg["enabled"] else " Forwarding is off."))
        return RedirectResponse("/admin/syslog", 303)

    @app.post("/admin/syslog/test")
    async def syslog_test(req: Request):
        user = admin(req)
        f = await form(req)
        try:
            cfg = syslog_from_form({**f, "enabled": "1"})
            await run_in_threadpool(syslog.send_test, cfg, user["full_name"])
            ok, msg = True, f"Test message sent to {cfg['host']}:{cfg['port']} over {cfg['transport'].upper()}."
            if cfg["transport"] == "udp":
                msg += " UDP can't confirm delivery; check the server received it."
        except Exception as e:
            ok, msg = False, f"Test failed: {e}"
        with ehr.session() as c:
            audit(c, user, "syslog_test", detail=msg, client=ctx.client(req))
        flash(req, msg, "info" if ok else "error")
        return RedirectResponse("/admin/syslog", 303)

    # ------------------------------------------------------------------ trust center
    @app.get("/admin/trust", response_class=HTMLResponse)
    async def trust_view(req: Request):
        user = admin(req)
        info, err = None, None
        try:
            info = trust.current(s.tls_cert, s.tls_key)
        except Exception as e:
            err = f"The installed certificate could not be read: {e}"
        pending = s.tls_key.with_name("pending.csr")
        prev = s.tls_cert.with_name(s.tls_cert.name + ".prev")
        prev_info = None
        if prev.exists():
            try:
                prev_info = trust.describe(prev.read_bytes())
            except Exception:
                prev_info = None
        return render(req, "trust.html", user, info=info, err=err, live=trust.LIVE_SSL is not None,
                      csr=pending.read_text() if pending.exists() else None, prev_info=prev_info)

    @app.post("/admin/trust/self-signed")
    async def trust_self_signed(req: Request):
        user = admin(req)
        f = await form(req)
        try:
            days = max(1, min(int(f.get("days") or 825), 3650))
            cert, key = await run_in_threadpool(trust.generate_self_signed, f.get("cn") or "drprepper.local",
                                                f.get("names", ""), days, f.get("kind", "ecdsa"), f.get("org", ""))
            info = _install_and_publish(s.tls_cert, s.tls_key, cert, key)
        except Exception as e:
            flash(req, f"Could not create the certificate: {e}", "error")
            return RedirectResponse("/admin/trust", 303)
        with ehr.session() as c:
            audit(c, user, "cert_generate", detail=f"{info.subject}, valid to {info.not_after:%Y-%m-%d}, SHA-256 {info.fingerprint[:23]}",
                  client=ctx.client(req))
        flash(req, "New certificate installed. " + ("It is in use now; devices must trust the new certificate."
                                                     if trust.LIVE_SSL else "Restart the web service to use it."))
        return RedirectResponse("/admin/trust", 303)

    @app.post("/admin/trust/csr")
    async def trust_csr(req: Request):
        user = admin(req)
        f = await form(req)
        try:
            csr, key = await run_in_threadpool(trust.generate_csr, f.get("cn") or "drprepper.local", f.get("names", ""),
                                               f.get("kind", "ecdsa"), f.get("org", ""))
        except Exception as e:
            flash(req, f"Could not create the request: {e}", "error")
            return RedirectResponse("/admin/trust", 303)
        from .config import write_private
        write_private(s.tls_key.with_name("pending.key"), key)
        write_private(s.tls_key.with_name("pending.csr"), csr)
        with ehr.session() as c:
            audit(c, user, "cert_csr", detail=f"CN={f.get('cn')}", client=ctx.client(req))
        flash(req, "Signing request created. Send it to your certificate authority, then upload the signed certificate below.")
        return RedirectResponse("/admin/trust#csr", 303)

    @app.get("/admin/trust/pending.csr")
    async def trust_csr_download(req: Request):
        admin(req)
        p = s.tls_key.with_name("pending.csr")
        if not p.exists():
            raise HTTPException(404, "No pending request")
        return Response(p.read_bytes(), media_type="application/pkcs10",
                        headers={"Content-Disposition": 'attachment; filename="drprepper.csr"'})

    @app.post("/admin/trust/install")
    async def trust_install(req: Request):
        user = admin(req)
        raw = await req.form()
        if raw.get("csrf") != req.session.get("csrf"):
            raise HTTPException(400, "This form expired. Reload the page and try again.")

        async def read(field: str) -> bytes:
            v = raw.get(field)
            if v is None or v == "":
                return b""
            if hasattr(v, "read"):
                return await v.read()
            return v.encode()

        cert = (await read("cert_file")) or (await read("cert_pem"))
        key = (await read("key_file")) or (await read("key_pem"))
        chain = (await read("chain_file")) or (await read("chain_pem"))
        pending_key = s.tls_key.with_name("pending.key")
        used_pending = False
        if not key and pending_key.exists():
            key, used_pending = pending_key.read_bytes(), True
        if not cert or not key:
            flash(req, "Upload the signed certificate (and its private key, unless it answers this unit's signing request).", "error")
            return RedirectResponse("/admin/trust", 303)
        full = cert.strip() + b"\n" + (chain.strip() + b"\n" if chain else b"")
        try:
            info = _install_and_publish(s.tls_cert, s.tls_key, full, key)
        except ValueError as e:
            flash(req, str(e), "error")
            return RedirectResponse("/admin/trust", 303)
        if used_pending:
            pending_key.unlink(missing_ok=True)
            s.tls_key.with_name("pending.csr").unlink(missing_ok=True)
        with ehr.session() as c:
            audit(c, user, "cert_install", detail=f"{info.subject}, issued by {info.issuer}, valid to {info.not_after:%Y-%m-%d}",
                  client=ctx.client(req))
        flash(req, "Certificate installed" + (" and in use now." if trust.LIVE_SSL else ". Restart the web service to use it."))
        return RedirectResponse("/admin/trust", 303)

    @app.post("/admin/trust/rollback")
    async def trust_rollback(req: Request):
        user = admin(req)
        await form(req)
        pc, pk = (s.tls_cert.with_name(s.tls_cert.name + ".prev"), s.tls_key.with_name(s.tls_key.name + ".prev"))
        if not (pc.exists() and pk.exists()):
            flash(req, "There is no previous certificate to go back to.", "error")
            return RedirectResponse("/admin/trust", 303)
        try:
            info = _install_and_publish(s.tls_cert, s.tls_key, pc.read_bytes(), pk.read_bytes())
        except ValueError as e:
            flash(req, f"The previous certificate can't be used: {e}", "error")
            return RedirectResponse("/admin/trust", 303)
        with ehr.session() as c:
            audit(c, user, "cert_install", detail=f"rolled back to previous: {info.subject}, valid to {info.not_after:%Y-%m-%d}",
                  client=ctx.client(req))
        flash(req, "Previous certificate restored.")
        return RedirectResponse("/admin/trust", 303)

    # ------------------------------------------------------------------ backups
    @app.get("/admin/backups", response_class=HTMLResponse)
    async def backups_view(req: Request):
        user = admin(req)
        with ehr.session() as c:
            cfg = backup.load_config(c, s.backup_default_dest)
            runs = c.execute("SELECT * FROM backup_runs ORDER BY id DESC LIMIT 30").fetchall()
            last = get_setting(c, "backup_last", {}) or {}
        ref = datetime.now()
        nxt = {k: backup.next_due(k, cfg[k], ref) if cfg[k]["enabled"] else None for k in backup.KINDS}
        with ehr.session() as c:
            due_now = set(backup.due_kinds(c, ref, s.backup_default_dest))
        files = backup.list_bundles(cfg["dest"])
        return render(req, "backups.html", user, cfg=cfg, runs=runs, last=last, nxt=nxt, files=files,
                      safety=backup.pre_restore_copies(s),
                      warn=backup.dest_warning(cfg["dest"], s.state_dir), weekdays=backup.WEEKDAYS,
                      has_pass=bool(cfg["passphrase"]), verify=req.session.pop("bk_verify", None), due_now=due_now)

    @app.post("/admin/backups/safety-delete")
    async def backups_safety_delete(req: Request):
        user = admin(req)
        f = await form(req)
        name = f.get("name") or ""
        if (f.get("confirm") or "").strip() != "DELETE":
            flash(req, "Type DELETE to confirm removing the safety copy.", "error")
            return RedirectResponse("/admin/backups#safety", 303)
        try:
            backup.delete_pre_restore(s, name)
        except (backup.BackupError, OSError) as e:
            flash(req, f"Couldn't delete it: {e}", "error")
            return RedirectResponse("/admin/backups#safety", 303)
        with ehr.session() as c:
            audit(c, user, "backup_safety_delete", detail=name, client=ctx.client(req))
        flash(req, f"Deleted the safety copy {name} and its key.")
        return RedirectResponse("/admin/backups#safety", 303)

    @app.post("/admin/backups/settings")
    async def backups_save(req: Request):
        user = admin(req)
        f = await form(req)
        with ehr.session() as c:
            cfg = backup.load_config(c, s.backup_default_dest)
            new = dict(cfg)
            new["dest"] = f.get("dest", "").rstrip("/") or ""
            new["include_drugs"] = f.get("include_drugs") == "1"
            for k in backup.KINDS:
                sch = dict(cfg[k])
                sch["enabled"] = f.get(f"{k}_enabled") == "1"
                t = f.get(f"{k}_time") or sch["time"]
                try:
                    datetime.strptime(t, "%H:%M")
                except ValueError:
                    flash(req, f"{k.title()} time must be HH:MM.", "error")
                    return RedirectResponse("/admin/backups", 303)
                sch["time"] = t
                sch["keep"] = max(1, min(int(f.get(f"{k}_keep") or sch["keep"]), 365))
                if k == "weekly":
                    sch["weekday"] = int(f.get("weekly_weekday") or sch["weekday"]) % 7
                if k == "monthly":
                    sch["day"] = max(1, min(int(f.get("monthly_day") or sch["day"]), 28))
                new[k] = sch
            p1, p2 = f.get("passphrase", ""), f.get("passphrase2", "")
            if p1 or p2:
                if p1 != p2:
                    flash(req, "The passphrases don't match.", "error")
                    return RedirectResponse("/admin/backups", 303)
                if len(p1) < backup.MIN_PASSPHRASE:
                    flash(req, f"The passphrase must be at least {backup.MIN_PASSPHRASE} characters.", "error")
                    return RedirectResponse("/admin/backups", 303)
                new["passphrase"] = p1
            set_setting(c, "backup", new)
            sched = ", ".join(f"{k} {new[k]['time']} keep {new[k]['keep']}" for k in backup.KINDS if new[k]["enabled"]) or "no schedules"
            audit(c, user, "backup_settings", detail=f"destination {new['dest'] or '(none)'}; {sched}"
                  + ("; passphrase changed" if p1 else ""), client=ctx.client(req))
        flash(req, "Backup settings saved." + (" Write the passphrase down and keep it somewhere safe: backups can't be restored without it."
                                                if p1 else ""))
        return RedirectResponse("/admin/backups", 303)

    @app.post("/admin/backups/run")
    async def backups_run(req: Request):
        user = admin(req)
        await form(req)
        res = await run_in_threadpool(backup.run_backup, ehr, "manual", user)
        if res["ok"]:
            flash(req, f"Backup written: {res['file']} ({res['size'] / 1e6:.1f} MB).")
        else:
            flash(req, f"Backup failed: {res['error']}", "error")
        return RedirectResponse("/admin/backups", 303)

    @app.post("/admin/backups/download")
    async def backups_download(req: Request):
        user = admin(req)
        await form(req)
        with ehr.session() as c:
            cfg = backup.load_config(c, s.backup_default_dest)
        tmp = Path(tempfile.mkdtemp(prefix="drp-dl-", dir=s.state_dir))
        try:
            path = await run_in_threadpool(backup.create_bundle, ehr, tmp, "download", cfg["passphrase"], cfg["include_drugs"])
        except backup.BackupError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            flash(req, str(e), "error")
            return RedirectResponse("/admin/backups", 303)
        with ehr.session() as c:
            audit(c, user, "backup_download", detail=f"{path.name}, {path.stat().st_size / 1e6:.1f} MB", client=ctx.client(req))
        return FileResponse(path, filename=path.name, media_type="application/octet-stream",
                            background=BackgroundTask(shutil.rmtree, tmp, True))

    def bundle_path(cfg: dict, name: str) -> Path:
        p = Path(cfg["dest"]) / name
        if "/" in name or not name.endswith(".drpbak") or not p.is_file():
            raise HTTPException(404, "Backup not found at the destination.")
        return p

    async def uploaded(raw, tmpdir: Path) -> Path | None:
        up = raw.get("bundle")
        if up is None or not hasattr(up, "read") or not getattr(up, "filename", ""):
            return None
        dest = tmpdir / Path(up.filename).name
        with open(dest, "wb") as out:
            while chunk := await up.read(1 << 20):
                out.write(chunk)
        return dest

    @app.post("/admin/backups/verify")
    async def backups_verify(req: Request):
        user = admin(req)
        raw = await req.form()
        if raw.get("csrf") != req.session.get("csrf"):
            raise HTTPException(400, "This form expired. Reload the page and try again.")
        with ehr.session() as c:
            cfg = backup.load_config(c, s.backup_default_dest)
        tmp = Path(tempfile.mkdtemp(prefix="drp-up-", dir=s.state_dir))
        try:
            path = await uploaded(raw, tmp) or bundle_path(cfg, raw.get("name", ""))
            pw = raw.get("passphrase") or cfg["passphrase"]
            res = await run_in_threadpool(backup.verify_bundle, ehr, path, pw)
            m = res["manifest"]
            msg = (f"{res['file']}: checksums OK" + (", decrypted and opened, integrity OK, "
                   f"{res.get('patients', '?')} patients" if res["decrypted"] else "")
                   + f". Made {m['created'][:16]} on {m['source_host']}.")
            ok = True
        except (backup.BackupError, HTTPException) as e:
            msg, ok = f"Verification failed: {getattr(e, 'detail', e)}", False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        with ehr.session() as c:
            audit(c, user, "backup_verify", detail=msg, client=ctx.client(req))
        req.session["bk_verify"] = {"ok": ok, "msg": msg}
        return RedirectResponse("/admin/backups#verify", 303)

    @app.post("/admin/backups/restore")
    async def backups_restore(req: Request):
        user = admin(req)
        raw = await req.form()
        if raw.get("csrf") != req.session.get("csrf"):
            raise HTTPException(400, "This form expired. Reload the page and try again.")
        if (raw.get("confirm") or "").strip() != "RESTORE":
            flash(req, "Type RESTORE to confirm. Restoring replaces every record on this unit.", "error")
            return RedirectResponse("/admin/backups#restore", 303)
        with ehr.session() as c:
            cfg = backup.load_config(c, s.backup_default_dest)
        tmp = Path(tempfile.mkdtemp(prefix="drp-up-", dir=s.state_dir))
        try:
            path = await uploaded(raw, tmp) or bundle_path(cfg, raw.get("name", ""))
            res = await run_in_threadpool(backup.restore_bundle, ehr, path, raw.get("passphrase") or "",
                                          raw.get("restore_tls") == "1", user["full_name"])
        except (backup.BackupError, HTTPException) as e:
            flash(req, f"Restore failed, nothing was changed: {getattr(e, 'detail', e)}", "error")
            return RedirectResponse("/admin/backups#restore", 303)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        req.session.clear()
        req.session["flash"] = [["info", f"Restored backup from {res['manifest']['created'][:16]}. "
                                         f"Sign in with an account from that backup. Previous data kept in {res['kept']}."]]
        return RedirectResponse("/login", 303)
