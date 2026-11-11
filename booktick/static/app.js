// BookTick's page script. It loads once in <head>, before Gradio draws the page, so
// everything here waits for elements by looking them up when they're needed.
(() => {
  if (window.BookTick) return;
  const BT = (window.BookTick = { queue: [] });
  const ICONS = window.BT_ICONS || {};
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => {
      try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) {}
    },
  };

  // ---------------------------------------------------------------------------
  // Who you are. A random key kept in this browser marks which watchers are yours
  // (the server only keeps a hash of it). A link ending in #key=... carries it to
  // another device. The admin key, once entered, is remembered here too.
  // ---------------------------------------------------------------------------
  const KEY = "booktick-key";
  const ADMIN = "booktick-admin";
  const validKey = (k) => /^[0-9a-f]{32}$/.test(k || "");
  let pageKey = null;  // used when the browser blocks storage: lasts until the page closes
  const newKey = () => {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  };
  const linked = /(?:^#|&)key=([0-9a-f]{32})/.exec(location.hash);
  if (linked) {
    store.set(KEY, linked[1]);
    try { history.replaceState(null, "", location.pathname + location.search); } catch (e) {}
  }
  BT.key = () => {
    let k = store.get(KEY);
    if (!validKey(k)) {
      k = pageKey || newKey();
      pageKey = k;
      store.set(KEY, k);
    }
    return k;
  };
  BT.identity = () => JSON.stringify({ key: BT.key(), admin: store.get(ADMIN) || "" });
  // Python sends the admin key to keep after a successful unlock, or "-" to forget it.
  BT.storeAdmin = (k) => {
    if (k === "-") store.set(ADMIN, null);
    else if (k) store.set(ADMIN, k);
    return [];
  };
  BT.deviceLink = () => location.origin + location.pathname + "#key=" + BT.key();

  // Until the city, cinema and movie lists arrive, those pickers show a spinner and
  // can't be changed (see .bt-booting in CSS), so an early pick isn't overwritten.
  document.documentElement.classList.add("bt-booting");
  BT.booted = () => { document.documentElement.classList.remove("bt-booting"); return []; };
  setTimeout(BT.booted, 20000);

  // Buttons inside the HTML cards can't call Python directly. They queue
  // "action:watcher_id" and click a hidden Gradio button that reads the queue.
  BT.take = () => BT.queue.shift() || "";
  BT.send = (action, id) => {
    BT.queue.push(action + ":" + (id || ""));
    const btn = document.getElementById("bt-action-btn");
    if (btn) btn.click();
  };

  // Live regions (stats, watcher cards) are patched in place instead of being
  // re-rendered, so a button never disappears under the pointer mid-click.
  const morph = (a, b) => {
    if (a.nodeType !== b.nodeType || a.nodeName !== b.nodeName) { a.replaceWith(b.cloneNode(true)); return; }
    if (a.nodeType !== 1) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }
    for (const at of Array.from(a.attributes)) if (!b.hasAttribute(at.name)) a.removeAttribute(at.name);
    for (const at of Array.from(b.attributes)) if (a.getAttribute(at.name) !== at.value) a.setAttribute(at.name, at.value);
    morphChildren(a, b);
  };
  const morphChildren = (a, b) => {
    const old = Array.from(a.childNodes), next = Array.from(b.childNodes);
    next.forEach((n, i) => { if (i < old.length) morph(old[i], n); else a.appendChild(n.cloneNode(true)); });
    for (let i = next.length; i < old.length; i++) old[i].remove();
  };
  BT.render = (region, html, tries) => {
    const root = document.querySelector('.bt-live-root[data-region="' + region + '"]');
    if (!root) {
      if ((tries || 0) < 50) setTimeout(() => BT.render(region, html, (tries || 0) + 1), 100);
      return [];
    }
    const tpl = document.createElement("template");
    tpl.innerHTML = html || "";
    morphChildren(root, tpl.content);
    return [];
  };

  BT.goto = (tab) => {
    const btn = document.getElementById("tab-" + tab + "-button");
    if (btn) btn.click();
    const tabs = document.getElementById("bt-tabs");
    if (tabs && tabs.getBoundingClientRect().top < 0) tabs.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  // A small confirm dialog placed next to the button that opened it, so it stays
  // in view even when Hugging Face stretches the app's frame to its full height.
  BT.confirm = (anchor, title, message, okLabel) => new Promise((resolve) => {
    const shade = document.createElement("div");
    shade.className = "bt-shade";
    const box = document.createElement("div");
    box.className = "bt-dialog";
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-modal", "true");
    box.innerHTML = '<div class="bt-dialog-icon">' + (ICONS.trash || "") + "</div><h3></h3><p></p>" +
      '<div class="bt-dialog-actions"><button type="button" class="bt-btn" data-x="no">Cancel</button>' +
      '<button type="button" class="bt-btn danger-solid" data-x="yes"></button></div>';
    box.querySelector("h3").textContent = title;
    box.querySelector("p").textContent = message;
    box.querySelector('[data-x="yes"]').textContent = okLabel;
    const width = Math.min(380, window.innerWidth - 32);
    box.style.width = width + "px";
    box.style.left = (window.scrollX + Math.max(16, (window.innerWidth - width) / 2)) + "px";
    document.body.appendChild(shade);
    document.body.appendChild(box);
    const r = anchor.getBoundingClientRect();
    const top = window.scrollY + r.top + r.height / 2 - box.offsetHeight / 2;
    box.style.top = Math.max(window.scrollY + 16, top) + "px";
    const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); close(false); } };
    const close = (ok) => {
      shade.remove(); box.remove();
      document.removeEventListener("keydown", onKey, true);
      if (document.body.contains(anchor)) anchor.focus();
      resolve(ok);
    };
    shade.addEventListener("click", () => close(false));
    box.addEventListener("click", (e) => {
      const x = e.target.closest("[data-x]");
      if (x) close(x.getAttribute("data-x") === "yes");
    });
    document.addEventListener("keydown", onKey, true);
    box.querySelector('[data-x="no"]').focus();
  });

  // Copies text, then marks the button for a moment so you can see it worked.
  BT.copy = (text, button) => {
    const done = () => {
      if (!button) return;
      button.classList.add("is-copied");
      setTimeout(() => button.classList.remove("is-copied"), 1600);
    };
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) {}
      ta.remove();
      done();
    };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  };
  BT.copyLog = () => {
    const rows = Array.from(document.querySelectorAll("#bt-log .bt-log-row"));
    BT.copy(rows.map((r) => r.getAttribute("data-line") || r.textContent).join("\n"), document.getElementById("bt-copy-log"));
    return [];
  };

  document.addEventListener("click", (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (!t) return;
    const filter = t.closest(".bt-chip[data-bt-filter]");
    if (filter) {
      document.documentElement.setAttribute("data-bt-view", filter.getAttribute("data-bt-filter"));
      return;
    }
    const go = t.closest("[data-bt-goto]");
    if (go) { e.preventDefault(); BT.goto(go.getAttribute("data-bt-goto")); return; }
    const link = t.closest("[data-bt-device-link]");
    if (link) { e.preventDefault(); BT.copy(BT.deviceLink(), link); return; }
    const a = t.closest("[data-bt-action]");
    if (!a) return;
    e.preventDefault();
    const action = a.getAttribute("data-bt-action");
    const id = a.getAttribute("data-bt-id") || "";
    const card = a.closest(".bt-card");
    const run = () => { if (card) card.classList.add("is-busy"); BT.send(action, id); };
    const ask = a.getAttribute("data-bt-confirm");
    if (!ask) return run();
    BT.confirm(a, a.getAttribute("data-bt-confirm-title") || "Are you sure?", ask,
               a.getAttribute("data-bt-confirm-ok") || "Confirm").then((ok) => { if (ok) run(); });
  });

  // ---------------------------------------------------------------------------
  // Light/dark switch. Gradio picks the theme itself when the page loads (and again
  // when the system theme changes), so a saved choice is re-applied on top of it.
  // ---------------------------------------------------------------------------
  const THEME = "booktick-theme";
  const applyTheme = () => {
    const mode = store.get(THEME);
    if (!document.body || (mode !== "dark" && mode !== "light")) return;
    const dark = mode === "dark";
    if (document.body.classList.contains("dark") !== dark) document.body.classList.toggle("dark", dark);
  };
  const watchTheme = () => {
    applyTheme();
    new MutationObserver(applyTheme).observe(document.body, { attributes: true, attributeFilter: ["class"] });
  };
  if (document.body) watchTheme(); else document.addEventListener("DOMContentLoaded", watchTheme);
  document.addEventListener("click", (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (!t || !t.closest("[data-bt-theme]")) return;
    const next = document.body.classList.contains("dark") ? "light" : "dark";
    store.set(THEME, next);
    document.body.classList.toggle("dark", next === "dark");
  });

  // ---------------------------------------------------------------------------
  // Show-date calendar (markup from views.date_picker_html). The pick lives in the
  // hidden #bt-date textbox as "YYYY-MM-DD", or "YYYY-MM-DD..YYYY-MM-DD" for a few
  // days. Dates are handled as UTC calendar days, so the browser's timezone never
  // shifts them, and "today" always means today in India.
  // ---------------------------------------------------------------------------
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December"];
  const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const MONTHS_AHEAD = 12;
  const MAX_RANGE = 7;  // matches MAX_RANGE_DAYS in config.py

  const pad = (n) => String(n).padStart(2, "0");
  const toIso = (d) => d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate());
  const fromIso = (s) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || "").trim());
    return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
  };
  const today = () => new Date(Date.now() + 330 * 60000).toISOString().slice(0, 10);  // IST is UTC+5:30
  const addDays = (s, n) => { const d = fromIso(s); d.setUTCDate(d.getUTCDate() + n); return toIso(d); };
  const monthOf = (s) => s.slice(0, 7);
  const addMonths = (ym, n) => toIso(new Date(Date.UTC(+ym.slice(0, 4), +ym.slice(5, 7) - 1 + n, 1))).slice(0, 7);
  const daysIn = (ym) => new Date(Date.UTC(+ym.slice(0, 4), +ym.slice(5, 7), 0)).getUTCDate();
  const dayMonth = (d) => pad(d.getUTCDate()) + " " + MONTHS[d.getUTCMonth()].slice(0, 3);
  const short = (s) => { const d = fromIso(s); return DAYS[d.getUTCDay()].slice(0, 3) + " " + dayMonth(d); };
  // Same wording as dates_label() in core.py.
  const label = (a, b) => {
    const t = today();
    if (!b || a === b) {
      if (a === t) return "Today · " + short(a);
      if (a === addDays(t, 1)) return "Tomorrow · " + short(a);
      return short(a);
    }
    const A = fromIso(a), B = fromIso(b);
    const sameMonth = A.getUTCMonth() === B.getUTCMonth() && A.getUTCFullYear() === B.getUTCFullYear();
    const left = DAYS[A.getUTCDay()].slice(0, 3) + " " + (sameMonth ? pad(A.getUTCDate()) : dayMonth(A));
    return left + " – " + short(b) + " · " + (Math.round((B - A) / 864e5) + 1) + " days";
  };
  const parse = (v) => {
    const [a, b] = String(v || "").trim().split("..");
    if (!fromIso(a)) return null;
    const end = fromIso(b) ? b.trim() : a.trim();
    return end < a ? { start: end, end: a.trim() } : { start: a.trim(), end };
  };

  const field = () => document.querySelector("#bt-date input, #bt-date textarea");
  const picked = () => { const el = field(); return parse(el && el.value) || { start: today(), end: today() }; };
  const showLabel = (root, p) => {
    const out = root.querySelector(".bt-date-value");
    if (out) out.textContent = label(p.start, p.end);
  };

  const render = (root, focus) => {
    const cal = root.querySelector(".bt-cal");
    const t = today(), sel = picked(), ym = cal.dataset.month;
    const mode = root.dataset.mode || "day";
    const anchor = mode === "range" ? root.dataset.anchor || "" : "";
    const lastAllowed = anchor ? addDays(anchor, MAX_RANGE - 1) : "";
    // While the first day of a range is picked, only it is shown; otherwise the saved pick.
    const from = anchor || sel.start, to = anchor || sel.end;
    const lead = fromIso(ym + "-01").getUTCDay();
    const count = daysIn(ym);
    const name = MONTHS[+ym.slice(5, 7) - 1] + " " + ym.slice(0, 4);
    const usable = (s) => s >= t && (!lastAllowed || s <= lastAllowed);
    // The one day reachable with Tab; arrow keys move from there.
    const target = [focus, from, t, ym + "-01"].find((s) => s && monthOf(s) === ym && usable(s));
    cal.querySelector(".bt-cal-title").textContent = name;
    cal.querySelector('[data-cal-nav="-1"]').disabled = ym <= monthOf(t);
    cal.querySelector('[data-cal-nav="1"]').disabled = ym >= addMonths(monthOf(t), MONTHS_AHEAD);
    cal.querySelectorAll("[data-cal-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.calMode === mode)));
    cal.querySelector(".bt-cal-hint").textContent = mode !== "range" ? ""
      : anchor ? "Now pick the last day (up to " + MAX_RANGE + " days)."
      : sel.start !== sel.end ? "Tap a day to start a new range." : "Pick the first day.";

    let html = "";
    for (let i = 0; i < Math.ceil((lead + count) / 7) * 7; i++) {
      const n = i - lead + 1;
      if (n < 1 || n > count) { html += "<span></span>"; continue; }
      const s = ym + "-" + pad(n);
      const inside = s >= from && s <= to;
      const edge = s === from || s === to;
      const cls = "bt-day" + (s === t ? " is-today" : "") + (inside && edge ? " is-selected" : "") +
        (inside && !edge ? " is-in-range" : "") + (from !== to && s === from ? " is-range-start" : "") +
        (from !== to && s === to ? " is-range-end" : "");
      html += '<button type="button" class="' + cls + '" data-date="' + s + '" tabindex="' + (s === target ? 0 : -1) + '"' +
        ' aria-pressed="' + inside + '"' + (s === t ? ' aria-current="date"' : "") + (usable(s) ? "" : " disabled") +
        ' aria-label="' + DAYS[i % 7] + " " + n + " " + name + '">' + n + "</button>";
    }
    cal.querySelector(".bt-cal-days").innerHTML = html;

    // Shortcuts: today, tomorrow, the next Friday (when new movies usually release), the weekend.
    const dow = fromIso(t).getUTCDay();
    let k = (5 - dow + 7) % 7;
    if (k < 2) k += 7;
    const fri = addDays(t, k);
    const sat = dow === 0 ? t : addDays(t, (6 - dow + 7) % 7);
    const sun = dow === 0 ? t : addDays(sat, 1);
    const quick = [["Today", t, t, ""], ["Tomorrow", addDays(t, 1), addDays(t, 1), ""],
                   [short(fri), fri, fri, "Next Friday, when new movies usually release"],
                   ["Weekend", sat, sun, "Saturday and Sunday"]];
    cal.querySelector(".bt-cal-quick").innerHTML = quick.map(([text, a, b, title]) =>
      '<button type="button" class="bt-cal-chip' + (a === sel.start && b === sel.end ? " is-selected" : "") +
      '" data-cal-pick="' + (a === b ? a : a + ".." + b) + '"' + (title ? ' title="' + title + '"' : "") + ">" + text + "</button>").join("");
  };

  const focusDay = (root) => {
    const day = root.querySelector('.bt-day[tabindex="0"]');
    if (day) day.focus({ preventScroll: true });
  };
  const open = (root) => {
    const cal = root.querySelector(".bt-cal");
    const t = today(), sel = picked();
    root.dataset.mode = sel.start !== sel.end ? "range" : root.dataset.mode || "day";
    root.dataset.anchor = "";
    cal.dataset.month = monthOf(sel.start < t ? t : sel.start);
    render(root);
    cal.classList.remove("is-right");
    cal.hidden = false;
    root.classList.add("is-open");
    root.querySelector(".bt-date-trigger").setAttribute("aria-expanded", "true");
    if (cal.getBoundingClientRect().right > document.documentElement.clientWidth - 8) cal.classList.add("is-right");
    focusDay(root);
  };
  const close = (root, refocus) => {
    const cal = root.querySelector(".bt-cal");
    if (!cal || cal.hidden) return;
    cal.hidden = true;
    root.classList.remove("is-open");
    root.dataset.anchor = "";
    const trigger = root.querySelector(".bt-date-trigger");
    trigger.setAttribute("aria-expanded", "false");
    if (refocus) trigger.focus();
  };
  const choose = (root, a, b) => {
    const el = field();
    const value = a === b ? a : a + ".." + b;
    if (el) {
      el.value = value;
      el.dispatchEvent(new Event("input", { bubbles: true }));  // tells Gradio the value changed
    }
    showLabel(root, { start: a, end: b });
    close(root, true);
  };
  const moveTo = (root, s) => {
    const t = today();
    const anchor = root.dataset.mode === "range" ? root.dataset.anchor : "";
    if (s < t) s = t;
    if (anchor && s > addDays(anchor, MAX_RANGE - 1)) return;
    if (monthOf(s) > addMonths(monthOf(t), MONTHS_AHEAD)) return;
    root.querySelector(".bt-cal").dataset.month = monthOf(s);
    render(root, s);
    focusDay(root);
  };

  BT.date = {
    sync: (v) => {
      document.querySelectorAll(".bt-date").forEach((root) => {
        showLabel(root, parse(v) || { start: today(), end: today() });
        if (root.classList.contains("is-open")) render(root);
      });
      return [];
    },
  };

  document.addEventListener("click", (e) => {
    const t = e.target instanceof Element ? e.target : null;
    const root = t && t.closest(".bt-date");
    document.querySelectorAll(".bt-date.is-open").forEach((r) => { if (r !== root) close(r); });
    if (!root) return;
    if (t.closest(".bt-date-trigger")) {
      if (root.classList.contains("is-open")) close(root); else open(root);
      return;
    }
    const nav = t.closest("[data-cal-nav]");
    if (nav) {
      if (!nav.disabled) {
        const cal = root.querySelector(".bt-cal");
        cal.dataset.month = addMonths(cal.dataset.month, +nav.getAttribute("data-cal-nav"));
        render(root);
      }
      return;
    }
    const mode = t.closest("[data-cal-mode]");
    if (mode) {
      root.dataset.mode = mode.getAttribute("data-cal-mode");
      root.dataset.anchor = "";
      render(root);
      return;
    }
    const chip = t.closest("[data-cal-pick]");
    if (chip) {
      const [a, b] = chip.getAttribute("data-cal-pick").split("..");
      choose(root, a, b || a);
      return;
    }
    const day = t.closest(".bt-day");
    if (!day || day.disabled) return;
    const s = day.getAttribute("data-date");
    if ((root.dataset.mode || "day") === "day") { choose(root, s, s); return; }
    const anchor = root.dataset.anchor;
    if (!anchor || s < anchor) {
      root.dataset.anchor = s;  // first day of the range; wait for the last
      render(root, s);
      focusDay(root);
      return;
    }
    choose(root, anchor, s);
  });

  document.addEventListener("keydown", (e) => {
    const t = e.target instanceof Element ? e.target : null;
    const root = t && t.closest(".bt-date");
    if (!root) return;
    if (e.key === "Escape" && root.classList.contains("is-open")) {
      e.preventDefault();
      close(root, true);
      return;
    }
    if (e.key === "ArrowDown" && t.closest(".bt-date-trigger") && !root.classList.contains("is-open")) {
      e.preventDefault();
      open(root);
      return;
    }
    const day = t.closest(".bt-day");
    if (!day) return;
    const s = day.getAttribute("data-date");
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key];
    if (step) {
      e.preventDefault();
      moveTo(root, addDays(s, step));
    } else if (e.key === "PageUp" || e.key === "PageDown") {
      e.preventDefault();
      const ym = addMonths(monthOf(s), e.key === "PageUp" ? -1 : 1);
      moveTo(root, ym + "-" + pad(Math.min(+s.slice(8), daysIn(ym))));
    }
  });

  // Tabbing out of the calendar closes it.
  document.addEventListener("focusin", (e) => {
    document.querySelectorAll(".bt-date.is-open").forEach((r) => { if (!r.contains(e.target)) close(r); });
  });
})();

// Picking a different city or cinema: select the current name on focus so typing
// replaces it, instead of backspacing the whole name first.
(() => {
  document.addEventListener("focusin", (e) => {
    const input = e.target;
    if (!(input instanceof HTMLInputElement) || !input.closest(".bt-dd") || input.closest("#bt-movie")) return;
    setTimeout(() => { if (document.activeElement === input && input.value) input.select(); }, 0);
  });
})();

// The header's "How to use" button opens the How to use tab.
document.addEventListener("click", (e) => {
  const go = e.target.closest("[data-bt-goto]");
  if (!go) return;
  const tab = document.getElementById(go.dataset.btGoto + "-button");
  if (tab) { tab.click(); document.getElementById("bt-tabs").scrollIntoView({ behavior: "smooth", block: "start" }); }
});

// On phones the tab bar scrolls sideways: keep the open tab in view, and fade the
// right edge only while more tabs are hidden there.
(() => {
  const nav = () => document.querySelector("#bt-tabs > .tab-nav");
  const fade = () => {
    const n = nav();
    if (n) n.classList.toggle("bt-scrolled-end", n.scrollLeft + n.clientWidth >= n.scrollWidth - 4);
  };
  // Gradio rebuilds the tab buttons when the tab changes, so look up the selected one afterwards.
  const reveal = () => {
    const n = nav(), btn = n && n.querySelector("button.selected");
    if (!btn || n.scrollWidth <= n.clientWidth) return;
    const left = btn.getBoundingClientRect().left - n.getBoundingClientRect().left + n.scrollLeft, right = left + btn.offsetWidth;
    if (left < n.scrollLeft) n.scrollTo({ left: left - 8 });
    else if (right > n.scrollLeft + n.clientWidth) n.scrollTo({ left: right - n.clientWidth + 8 });
    fade();
  };
  document.addEventListener("click", (e) => {
    if (e.target.closest("#bt-tabs > .tab-nav button")) setTimeout(reveal, 50);
  });
  document.addEventListener("scroll", (e) => { if (e.target === nav()) fade(); }, true);
  window.addEventListener("resize", fade);
  const wait = setInterval(() => { if (nav()) { clearInterval(wait); fade(); } }, 300);
})();
