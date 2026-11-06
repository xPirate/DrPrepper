// Dr.Prepper web console helpers. Loaded from /static (CSP: script-src 'self').
(function () {
  "use strict";

  function localNow() {
    const d = new Date();
    d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    return d.toISOString().slice(0, 16);
  }
  function csrf() {
    const m = document.querySelector('meta[name="csrf"]');
    return m ? m.content : "";
  }

  // ---------------------------------------------------------------- allergy badge popup
  // The list is fetched on click (never embedded in the page) so every view is recorded server-side.
  function allergyPopup(pid, btn) {
    const dlg = document.getElementById("allergy-dialog");
    if (!dlg) return;
    const who = dlg.querySelector(".ad-who"), list = dlg.querySelector(".ad-list"), none = dlg.querySelector(".ad-none");
    who.textContent = "Loading…"; list.innerHTML = ""; none.textContent = "";
    dlg.className = "allergy-dialog";
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
    fetch("/api/patients/" + encodeURIComponent(pid) + "/allergies", {
      method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf() }
    })
      .then(function (r) {
        if (r.status === 401 || r.redirected) { location.href = "/login"; throw new Error("signed out"); }
        if (!r.ok) throw new Error("Could not load allergies (" + r.status + "). Reload the page.");
        return r.json();
      })
      .then(function (d) {
        dlg.classList.add(d.status);
        who.textContent = d.name + " · patient " + d.pnum;
        if (d.status === "allergies") {
          d.items.forEach(function (a) { const li = document.createElement("li"); li.textContent = a; list.appendChild(li); });
        } else if (d.status === "nkda") {
          none.textContent = "No known drug allergies recorded.";
        } else {
          none.textContent = "Allergies have not been recorded or are unknown. Ask the patient or check their belongings before giving medications.";
        }
      })
      .catch(function (e) { who.textContent = e.message; });
  }

  document.addEventListener("click", function (ev) {
    const b = ev.target.closest("[data-allergy]");
    if (b) { ev.preventDefault(); allergyPopup(b.getAttribute("data-allergy"), b); return; }
    if (ev.target.closest("#allergy-dialog [data-close]")) {
      const dlg = document.getElementById("allergy-dialog");
      if (dlg.close) dlg.close(); else dlg.removeAttribute("open");
    }
  });
  document.addEventListener("click", function (ev) {   // click outside the dialog box closes it
    const dlg = document.getElementById("allergy-dialog");
    if (dlg && ev.target === dlg && dlg.close) dlg.close();
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("input[data-now]").forEach(function (el) { if (!el.value) el.value = localNow(); });

    document.querySelectorAll("form[data-confirm]").forEach(function (f) {
      f.addEventListener("submit", function (ev) { if (!window.confirm(f.getAttribute("data-confirm"))) ev.preventDefault(); });
    });
    document.querySelectorAll("[data-back]").forEach(function (a) {
      a.addEventListener("click", function (ev) { if (history.length > 1) { ev.preventDefault(); history.back(); } });
    });
    document.querySelectorAll("[data-print]").forEach(function (b) { b.addEventListener("click", function () { window.print(); }); });

    // Syslog: suggest the standard port when the transport changes.
    const tr = document.querySelector("select[data-port-defaults]");
    if (tr) {
      const port = tr.form.querySelector("input[name=port]");
      tr.addEventListener("change", function () {
        if (["514", "6514", ""].indexOf(port.value) >= 0) port.value = tr.value === "tls" ? "6514" : "514";
      });
    }

    // Drug pickers (medication given, home medication list).
    document.querySelectorAll("input[data-drug-search]").forEach(function (input) {
      const box = input.parentElement.querySelector("[data-suggest]");
      const idField = input.form.querySelector("input[data-drug-id]");
      let timer = null, seq = 0;
      input.addEventListener("input", function () {
        if (idField) idField.value = "";
        clearTimeout(timer);
        const q = input.value.trim();
        if (q.length < 2) { box.innerHTML = ""; return; }
        timer = setTimeout(function () {
          const my = ++seq;
          const prefer = input.getAttribute("data-drug-prefer");
          fetch("/api/drugs?q=" + encodeURIComponent(q) + (prefer ? "&prefer=" + encodeURIComponent(prefer) : ""), { credentials: "same-origin" })
            .then(function (r) { return r.ok ? r.json() : []; })
            .then(function (rows) {
              if (my !== seq) return;
              box.innerHTML = "";
              rows.forEach(function (row) {
                const b = document.createElement("button");
                b.type = "button";
                b.textContent = row.name + " — " + row.route + " (" + row.kind + ")";
                b.addEventListener("click", function () {
                  input.value = row.name; if (idField) idField.value = row.id;
                  const route = input.form.querySelector("input[name=route]");
                  if (route && !route.value && row.route) route.value = row.route.split(",")[0];
                  box.innerHTML = "";
                });
                box.appendChild(b);
              });
            })
            .catch(function () { box.innerHTML = ""; });
        }, 200);
      });
      document.addEventListener("click", function (ev) { if (!box.contains(ev.target) && ev.target !== input) box.innerHTML = ""; });
    });

    // Discharge form: show the parts that apply to the chosen outcome.
    const dc = document.querySelector("form[data-dc]");
    if (dc) {
      const sync = function () {
        const k = (dc.querySelector("input[name=kind]:checked") || {}).value || "";
        dc.querySelectorAll("[data-show]").forEach(function (el) {
          el.hidden = el.getAttribute("data-show").split(" ").indexOf(k) < 0;
        });
      };
      dc.querySelectorAll("input[name=kind]").forEach(function (r) { r.addEventListener("change", sync); });
      sync();
    }

    // Admin > Date & time: common-zone picker fills the field; compare this device's clock to the unit's.
    const tzp = document.querySelector("select[data-tz-pick]");
    if (tzp) tzp.addEventListener("change", function () { if (tzp.value) tzp.form.querySelector("input[name=timezone]").value = tzp.value; });
    const dclk = document.querySelector("[data-device-clock]");
    if (dclk) {
      const diff = Math.round((Date.now() - new Date(dclk.getAttribute("data-server"))) / 60000);
      dclk.textContent = "This device: " + new Date().toLocaleString() + (Math.abs(diff) >= 3 ? " — " + Math.abs(diff) + " min " + (diff > 0 ? "ahead of" : "behind") + " the unit clock" : " — matches the unit clock");
    }

    // Live timers (triage wait, time in location).
    function fmt(m) { return m < 60 ? m + " min" : Math.floor(m / 60) + " h " + String(m % 60).padStart(2, "0") + " min"; }
    function tick() {
      document.querySelectorAll(".timer[data-since]").forEach(function (el) {
        const t = new Date(el.getAttribute("data-since"));
        if (isNaN(t)) return;
        const m = Math.max(0, Math.floor((Date.now() - t) / 60000));
        const b = el.querySelector("b"); if (b) b.textContent = fmt(m);
        const w = parseInt(el.getAttribute("data-warn") || "0", 10), a = parseInt(el.getAttribute("data-alert") || "0", 10);
        el.classList.toggle("alert", a > 0 && m >= a);
        el.classList.toggle("warn", w > 0 && m >= w && !(a > 0 && m >= a));
      });
    }
    tick(); setInterval(tick, 20000);

    // Status board: refresh the body in place, keep a clock in the unit's time zone, flag a lost connection.
    const bd = document.querySelector("[data-board]");
    if (bd) {
      const every = (parseInt(bd.getAttribute("data-refresh"), 10) || 10) * 1000;
      const offset = parseInt(bd.getAttribute("data-offset"), 10) || 0;
      const clockEl = bd.querySelector("[data-board-clock]"), upd = bd.querySelector("[data-board-updated]");
      const hm = function () {
        const d = new Date(Date.now() + offset * 60000);
        return String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0");
      };
      let last = Date.now();
      setInterval(function () { clockEl.textContent = hm(); }, 5000);
      const refresh = function () {
        fetch("/board/data", { credentials: "same-origin", cache: "no-store" })
          .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
          .then(function (html) {
            document.getElementById("board-body").innerHTML = html;
            const m = document.querySelector("[data-board-mode]");
            if (m && m.getAttribute("data-board-mode") !== document.documentElement.getAttribute("data-mode")) { location.reload(); return; }
            last = Date.now(); upd.textContent = "updated " + hm(); upd.classList.remove("stale"); tick();
          })
          .catch(function (e) {
            if (String(e.message) === "403") { location.reload(); return; }
            const mins = Math.floor((Date.now() - last) / 60000);
            upd.textContent = "NO CONNECTION" + (mins ? " · " + mins + " min" : ""); upd.classList.add("stale");
          });
      };
      setInterval(refresh, every);
    }
  });
})();

// ---------------------------------------------------------------- triage: incoming alerts
(function () {
  "use strict";
  const roleMeta = document.querySelector('meta[name="role"]');
  const role = roleMeta ? roleMeta.content : "";
  if (role !== "triage" && role !== "admin") return;

  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  let chime = store("drp-chime") === "1";
  let actx = null;

  function beep() {
    if (!chime) return;
    try {
      actx = actx || new (window.AudioContext || window.webkitAudioContext)();
      [0, 0.25, 0.5].forEach(function (t) {
        const o = actx.createOscillator(), g = actx.createGain();
        o.frequency.value = 880; o.connect(g); g.connect(actx.destination);
        g.gain.setValueAtTime(0.0001, actx.currentTime + t);
        g.gain.exponentialRampToValueAtTime(0.3, actx.currentTime + t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, actx.currentTime + t + 0.2);
        o.start(actx.currentTime + t); o.stop(actx.currentTime + t + 0.22);
      });
    } catch (e) { /* audio unavailable */ }
  }

  function etaText(iso) {
    const mins = Math.round((new Date(iso) - new Date()) / 60000);
    const hhmm = iso.slice(11, 16);
    if (mins > 0) return "ETA " + hhmm + " · in " + mins + " min";
    if (mins === 0) return "ETA " + hhmm + " · due now";
    return "ETA " + hhmm + " · " + (-mins) + " min overdue";
  }
  function tickEta() {
    document.querySelectorAll("[data-eta]").forEach(function (el) {
      const iso = el.getAttribute("data-eta");
      if (!iso) return;
      el.textContent = etaText(iso);
      el.classList.toggle("overdue", new Date(iso) < new Date());
    });
  }

  let last = Number(store("drp-incoming-latest") || 0);
  const baseTitle = document.title;
  function poll() {
    fetch("/api/triage/status", { credentials: "same-origin" })
      .then(function (r) { return r.ok && !r.redirected ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        const badge = document.querySelector("[data-incoming]");
        if (badge) {
          badge.hidden = d.incoming === 0;
          badge.querySelector("[data-incoming-n]").textContent = d.incoming;
        }
        document.title = d.incoming ? "(" + d.incoming + " incoming) " + baseTitle : baseTitle;
        if (d.latest && d.latest > last) {
          const first = last === 0 && store("drp-incoming-latest") === null;
          last = d.latest; store("drp-incoming-latest", String(last));
          if (!first) {
            beep();
            if (location.pathname === "/") { location.hash = "incoming"; location.reload(); }
          }
        }
      })
      .catch(function () { /* server unreachable; try again next time */ });
  }

  document.addEventListener("DOMContentLoaded", function () {
    const btn = document.querySelector("[data-chime-toggle]");
    if (btn) {
      const label = function () { btn.textContent = "Alert sound: " + (chime ? "on" : "off"); };
      label();
      btn.addEventListener("click", function () {
        chime = !chime; store("drp-chime", chime ? "1" : "0"); label();
        if (chime) beep();   // also unlocks audio, which browsers allow only after a click
      });
    }
    tickEta(); setInterval(tickEta, 15000);
    poll(); setInterval(poll, 8000);
  });
})();
