"""Audit trail: readable entries, tamper-evident hash chain, search and export.

Every entry is written inside the caller's transaction (so it commits or rolls
back with the change it describes) and sealed into a SHA-256 chain right after
commit. SQLite serializes writers, so ids increase in commit order and sealing
in id order is deterministic. Editing or deleting a sealed entry breaks the
chain from that point, which `verify()` reports.
"""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import datetime

from .ehr import now, user_display

GENESIS = "0" * 64

# action -> (template, severity). Placeholders: {pnum} {target} {detail} {eid} {reason}
VERBS: dict[str, tuple[str, str]] = {
    "login_ok": ("logged in{detail_paren}", "info"),
    "login_fail": ("failed login{detail_paren}", "warning"),
    "login_locked": ("login blocked after repeated failures{detail_paren}", "warning"),
    "logout": ("logged out", "info"),
    "session_timeout": ("signed out after inactivity", "info"),
    "password_change": ("changed their password{detail_paren}", "info"),
    "patient_create": ("created patient record {pnum}", "info"),
    "patient_view": ("accessed patient record {pnum}", "info"),
    "patient_view_justified": ("accessed patient record {pnum} without assignment. Reason: {reason}", "notice"),
    "access_denied": ("was refused access to patient record {pnum}{detail_paren}", "warning"),
    "patient_update": ("updated patient record {pnum}{detail_paren}", "info"),
    "patient_search": ("searched patients{detail_paren}", "info"),
    "allergy_view": ("viewed allergies for patient {pnum}", "info"),
    "patient_assign": ("assigned patient {pnum} to {target}{detail_paren}", "info"),
    "patient_unassign": ("returned patient {pnum} to the triage queue", "info"),
    "patient_handoff": ("handed off patient {pnum} to {target}{detail_paren}", "info"),
    "encounter_open": ("opened encounter {eid} for patient {pnum}", "info"),
    "encounter_view": ("viewed encounter {eid} for patient {pnum}", "info"),
    "encounter_print": ("printed handoff summary for patient {pnum} (encounter {eid})", "info"),
    "encounter_update": ("updated encounter {eid} for patient {pnum}", "info"),
    "encounter_close": ("closed encounter {eid} for patient {pnum}", "info"),
    "encounter_reopen": ("reopened encounter {eid} for patient {pnum}", "notice"),
    "vitals_add": ("recorded vital signs for patient {pnum}", "info"),
    "med_given": ("recorded medication for patient {pnum}: {detail}", "info"),
    "med_override": ("recorded medication for patient {pnum} despite allergy alert: {detail}", "warning"),
    "allergy_alert": ("was shown an allergy alert for patient {pnum}: {detail}", "notice"),
    "med_response": ("updated medication response for patient {pnum}", "info"),
    "note_add": ("added a note for patient {pnum}", "info"),
    "user_create": ("created user {target}{detail_paren}", "notice"),
    "user_update": ("updated user {target}{detail_paren}", "notice"),
    "user_reset_pw": ("reset the password for {target}", "notice"),
    "user_enable": ("enabled user {target}", "notice"),
    "user_disable": ("disabled user {target}", "notice"),
    "user_signout": ("signed out all sessions of {target}{detail_paren}", "notice"),
    "audit_export": ("exported the audit log{detail_paren}", "notice"),
    "audit_verify": ("verified the audit chain{detail_paren}", "info"),
    "syslog_settings": ("changed syslog forwarding settings{detail_paren}", "notice"),
    "syslog_test": ("sent a syslog test message{detail_paren}", "info"),
    "cert_generate": ("generated a new self-signed certificate{detail_paren}", "notice"),
    "cert_csr": ("created a certificate signing request{detail_paren}", "notice"),
    "cert_install": ("installed a certificate{detail_paren}", "notice"),
    "backup_settings": ("changed backup settings{detail_paren}", "notice"),
    "backup_ok": ("completed a backup{detail_paren}", "info"),
    "backup_fail": ("backup failed{detail_paren}", "warning"),
    "backup_download": ("downloaded a backup{detail_paren}", "notice"),
    "backup_verify": ("verified a backup{detail_paren}", "info"),
    "restore": ("restored the EHR from a backup{detail_paren}", "warning"),
    "backup_safety_delete": ("deleted a pre-restore safety copy{detail_paren}", "notice"),
    "system_start": ("started the EHR service{detail_paren}", "info"),
    "intake_report": ("sent an incoming patient report to triage: patient {pnum}{detail_paren}", "notice"),
    "intake_rejected": ("touchscreen intake report was refused{detail_paren}", "warning"),
    "patient_arrived": ("marked incoming patient {pnum} as arrived", "info"),
    "intake_cancel": ("cancelled incoming patient {pnum}{detail_paren}", "notice"),
    "intake_settings": ("changed touchscreen intake settings{detail_paren}", "notice"),
    "branding_settings": ("changed branding and theme settings{detail_paren}", "notice"),
    "patient_move": ("moved patient {pnum}{detail_paren}", "info"),
    "patient_discharged": ("discharged patient {pnum}{detail_paren}", "notice"),
    "patient_ama": ("recorded patient {pnum} leaving against medical advice{detail_paren}", "notice"),
    "patient_transfer": ("transferred patient {pnum}{detail_paren}", "notice"),
    "patient_lwbs": ("recorded patient {pnum} leaving without being seen{detail_paren}", "notice"),
    "patient_deceased": ("recorded the death of patient {pnum}{detail_paren}", "notice"),
    "patient_readmit": ("readmitted patient {pnum}{detail_paren}", "notice"),
    "disposition_correct": ("corrected the disposition of patient {pnum}{detail_paren}", "warning"),
    "disposition_view": ("viewed the disposition record for patient {pnum}{detail_paren}", "info"),
    "disposition_print": ("printed the disposition record for patient {pnum}{detail_paren}", "info"),
    "location_settings": ("changed locations{detail_paren}", "notice"),
    "ehr_unlocked": ("unlocked the patient records at start-up{detail_paren}", "notice"),
    "ehr_unlock_failed": ("wrong unlock passphrase entered{detail_paren}", "warning"),
    "encryption_settings": ("changed how the records are unlocked{detail_paren}", "warning"),
    "timezone_settings": ("changed the time zone{detail_paren}", "notice"),
    "room_state": ("changed room status{detail_paren}", "info"),
    "board_settings": ("changed status board settings{detail_paren}", "notice"),
    "display_key_create": ("created a status board display key{detail_paren}", "notice"),
    "display_key_revoke": ("revoked a status board display key{detail_paren}", "notice"),
    "display_connect": ("status board screen connected{detail_paren}", "info"),
    "history_update": ("updated the medical history for patient {pnum}{detail_paren}", "info"),
    "home_med_add": ("added a home medication for patient {pnum}{detail_paren}", "info"),
    "home_med_update": ("updated a home medication for patient {pnum}{detail_paren}", "info"),
}


def render(at: str, who: str, action: str, *, pnum=None, target=None, detail=None, eid=None, reason=None) -> tuple[str, str]:
    tmpl, sev = VERBS.get(action, (action.replace("_", " ") + "{detail_paren}", "info"))
    text = tmpl.format(pnum=pnum or "?", target=target or "?", detail=detail or "", eid=eid or "?",
                       reason=reason or "(none given)", detail_paren=f" ({detail})" if detail else "")
    stamp = datetime.fromisoformat(at).strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp} - {who} {text}", sev


def audit(c, user: dict | None, action: str, *, entity: str = "", entity_id: int | None = None, pnum: str | None = None,
          target: str | None = None, detail: str = "", reason: str | None = None, eid: int | None = None,
          client: str | None = None) -> None:
    at = now()
    who = user_display(user)
    message, sev = render(at, who, action, pnum=pnum, target=target, detail=detail, eid=eid or entity_id, reason=reason)
    c.execute("""INSERT INTO audit(at, user_id, user_display, action, severity, entity, entity_id, pnum, reason, detail, client, message)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
              (at, (user or {}).get("id"), who, action, sev, entity, entity_id, pnum, reason,
               (detail or "")[:1000], client, message[:2000]))


def _digest(prev: str, r: dict) -> str:
    fields = [prev, r["at"], str(r["user_id"] or ""), r["user_display"] or "", r["action"], r["severity"] or "",
              r["entity"] or "", str(r["entity_id"] or ""), r["pnum"] or "", r["reason"] or "", r["detail"] or "",
              r["client"] or "", r["message"] or ""]
    return hashlib.sha256("\x1f".join(fields).encode()).hexdigest()


def seal(ehr) -> int:
    """Chain any unsealed entries. Returns how many were sealed."""
    with ehr.seal_lock:
        c = ehr.connect()
        try:
            rows = c.execute("SELECT * FROM audit WHERE hash IS NULL ORDER BY id").fetchall()
            if not rows:
                return 0
            last = c.execute("SELECT hash FROM audit WHERE hash IS NOT NULL AND id < ? ORDER BY id DESC LIMIT 1",
                             (rows[0]["id"],)).fetchone()
            prev = last["hash"] if last else GENESIS
            for r in rows:
                h = _digest(prev, r)
                c.execute("UPDATE audit SET prev_hash = ?, hash = ? WHERE id = ?", (prev, h, r["id"]))
                prev = h
            c.commit()
            return len(rows)
        finally:
            c.close()


def verify(c) -> dict:
    """Walk the whole chain. Reports the first broken entry, if any."""
    prev, n = GENESIS, 0
    for r in c.execute("SELECT * FROM audit WHERE hash IS NOT NULL ORDER BY id"):
        n += 1
        if r["prev_hash"] != prev or _digest(prev, r) != r["hash"]:
            return {"ok": False, "checked": n, "broken_id": r["id"], "broken_at": r["at"],
                    "message": f"Entry #{r['id']} ({r['at'][:19]}) does not match the chain. It or an entry before it was altered or deleted."}
        prev = r["hash"]
    return {"ok": True, "checked": n, "last_hash": prev,
            "message": f"All {n} entries verified. Chain head {prev[:16]}…"}


# ------------------------------------------------------------------ search / export
def query(c, *, user_id=None, pnum=None, action=None, severity=None, date_from=None, date_to=None, text=None,
          limit=500, offset=0) -> tuple[list[dict], int]:
    where, args = [], []
    if user_id:
        where.append("user_id = ?"); args.append(user_id)
    if pnum:
        where.append("pnum = ?"); args.append(pnum.strip())
    if action:
        where.append("action = ?"); args.append(action)
    if severity:
        where.append("severity = ?"); args.append(severity)
    if date_from:
        where.append("substr(at, 1, 10) >= ?"); args.append(date_from)
    if date_to:
        where.append("substr(at, 1, 10) <= ?"); args.append(date_to)
    if text:
        where.append("message LIKE ?"); args.append(f"%{text}%")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = c.execute(f"SELECT COUNT(*) AS n FROM audit{w}", args).fetchone()["n"]
    rows = c.execute(f"SELECT * FROM audit{w} ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    return rows, total


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "time", "user", "action", "severity", "patient_number", "reason", "detail", "client", "message", "hash"])
    for r in rows:
        w.writerow([r["id"], r["at"], r["user_display"], r["action"], r["severity"], r["pnum"] or "", r["reason"] or "",
                    r["detail"] or "", r["client"] or "", r["message"], r["hash"] or ""])
    return buf.getvalue()


# ------------------------------------------------------------------ review summary (what an admin should look at)
WATCH = {
    "login_fail": "failed logins", "login_locked": "lockouts", "patient_view_justified": "charts opened with a reason",
    "med_override": "allergy overrides", "ehr_unlock_failed": "wrong unlock passphrases", "disposition_correct": "corrected dispositions",
    "access_denied": "refused chart access",
}


def review(c, hours: int = 24, burst: int = 5) -> dict:
    """Counts of the events worth a look, plus anyone who opened 5+ unassigned charts within an hour."""
    since = datetime.fromtimestamp(datetime.now().timestamp() - hours * 3600).astimezone().isoformat(timespec="seconds")
    rows = c.execute(f"SELECT action, user_id, user_display, at FROM audit WHERE at >= ? AND action IN ({','.join('?' * len(WATCH))})",
                     (since, *WATCH)).fetchall()
    counts = {k: 0 for k in WATCH}
    by_user: dict = {}
    for r in rows:
        counts[r["action"]] += 1
        if r["action"] == "patient_view_justified":
            by_user.setdefault((r["user_id"], r["user_display"]), []).append(datetime.fromisoformat(r["at"]).timestamp())
    flags = []
    for (uid, name), times in by_user.items():
        times.sort()
        for i in range(len(times)):
            j = i
            while j + 1 < len(times) and times[j + 1] - times[i] <= 3600:
                j += 1
            if j - i + 1 >= burst:
                flags.append({"user_id": uid, "name": name, "n": j - i + 1})
                break
    return {"hours": hours, "counts": {WATCH[k]: v for k, v in counts.items() if v}, "flags": flags}
