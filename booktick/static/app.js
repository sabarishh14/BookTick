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
  BT.identity = () => JSON.stringify({
    key: BT.key(), admin: store.get(ADMIN) || "",
    name: store.get("booktick-name") || "", ntfy: store.get("booktick-ntfy") || "",  // last used, to fill in the forms
  });
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
    // A row someone opened (a <details>) stays open when fresh HTML arrives.
    const keepOpen = a.nodeName === "DETAILS";
    for (const at of Array.from(a.attributes)) {
      if (!b.hasAttribute(at.name) && !(keepOpen && at.name === "open")) a.removeAttribute(at.name);
    }
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
    // Bring the tab bar into view when it's scrolled away or far down (the header's buttons).
    const tabs = document.getElementById("bt-tabs");
    const top = tabs ? tabs.getBoundingClientRect().top : 0;
    if (tabs && (top < 0 || top > window.innerHeight * 0.6)) tabs.scrollIntoView({ behavior: "smooth", block: "start" });
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
    // A shortcut to one of the form's own buttons (the phone tip's "Send a test alert").
    const press = t.closest("[data-bt-press]");
    if (press) {
      e.preventDefault();
      const target = document.getElementById(press.getAttribute("data-bt-press"));
      if (target) { target.scrollIntoView({ behavior: "smooth", block: "center" }); target.click(); }
      return;
    }
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
    if (!(input instanceof HTMLInputElement) || !input.closest(".bt-dd")) return;
    setTimeout(() => { if (document.activeElement === input && input.value) input.select(); }, 0);
  });
})();

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

// On phones, tapping a dropdown scrolls it near the top of the screen, so its list
// opens below it with room to spare instead of covering the page above.
document.addEventListener("focusin", (e) => {
  const input = e.target;
  if (window.innerWidth > 640 || !(input instanceof HTMLInputElement) || !input.closest(".bt-dd")) return;
  const box = input.closest(".bt-dd");
  const top = box.getBoundingClientRect().top;
  if (top > window.innerHeight * 0.3) {
    window.scrollBy({ top: top - 72 });
    // Gradio places the list when it opens; nudge it to measure again after the scroll.
    requestAnimationFrame(() => window.dispatchEvent(new Event("scroll")));
  }
});

// ---------------------------------------------------------------------------
// Movie picker. Python sends the movies to pick from as JSON (#bt-movie-catalog):
// {here: [names at the picked cinema], now: [showing in the city], soon: [coming soon],
// langs: [languages, the city's first]}; each movie is {n: name, p: poster, l: languages,
// g: genres, c: rating, w: release label}. Picks go into a hidden textbox
// (#bt-movie-value) as a JSON list of names, which is what the Python handlers read.
// ---------------------------------------------------------------------------
(() => {
  const BT = window.BookTick;
  if (!BT || BT.movies) return;
  const S = { cat: null, picks: null, tab: "now", lang: "all", q: "" };
  const root = () => document.querySelector(".bt-mp");
  const field = () => document.querySelector("#bt-movie-value input, #bt-movie-value textarea");
  const esc = (t) => String(t == null ? "" : t).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const svg = (d) => '<svg class="bt-i" viewBox="0 0 24 24" aria-hidden="true">' + d + "</svg>";
  const ICON = {
    plus: '<path d="M12 5v14M5 12h14"/>', x: '<path d="M18 6 6 18M6 6l12 12"/>', check: '<path d="M20 6 9 17l-5-5"/>',
    film: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 7.5h4M3 12h18M3 16.5h4M17 7.5h4M17 16.5h4"/>',
  };
  const low = (s) => String(s).toLocaleLowerCase();
  const initial = (name) => (String(name).match(/[\p{L}\p{N}]/u) || ["?"])[0].toUpperCase();

  const picks = () => {
    if (S.picks === null) {
      const el = field();
      if (!el) return [];
      try {
        const v = JSON.parse(el.value || "[]");
        S.picks = Array.isArray(v) ? v.map(String).filter((x) => x.trim()) : [];
      } catch (e) {
        S.picks = String(el.value || "").split(",").map((x) => x.trim()).filter(Boolean);
      }
    }
    return S.picks;
  };
  const save = () => {
    const el = field();
    if (!el) return;
    el.value = JSON.stringify(picks());
    el.dispatchEvent(new Event("input", { bubbles: true }));  // tells Gradio the value changed
    if (BT.cinemas && BT.cinemas.refresh) BT.cinemas.refresh();  // cinemas showing these come first there
  };
  const isPicked = (name) => picks().some((p) => low(p) === low(name));
  const hereNames = () => (BT.cinemas && BT.cinemas.movieNames ? BT.cinemas.movieNames() : null);
  const isHere = (name) => {
    const names = hereNames();
    if (names) return names.has(low(name));
    return !!S.cat && S.cat.here.some((h) => low(h) === low(name));
  };
  const hereLabel = () => (BT.cinemas && BT.cinemas.count && BT.cinemas.count() > 1 ? "your cinemas" : "your cinema");
  const find = (name) => {
    if (!S.cat) return null;
    const n = low(name);
    const now = S.cat.now.find((m) => low(m.n) === n);
    if (now) return { m: now, kind: "now" };
    const soon = S.cat.soon.find((m) => low(m.n) === n);
    return soon ? { m: soon, kind: "soon" } : null;
  };
  const poster = (m, name, extra) => '<span class="bt-mp-poster" data-initial="' + esc(initial(name)) + '">' +
    (m && m.p ? '<img src="' + esc(m.p) + '" alt="" loading="lazy" decoding="async">' : "") + (extra || "") + "</span>";

  // --- The picks, as poster chips ---------------------------------------------
  const renderPicked = (r) => {
    const box = r.querySelector(".bt-mp-picked");
    const list = picks();
    const chips = list.map((name) => {
      const hit = find(name);
      const meta = !S.cat ? "" : !hit ? "Not on District yet · watching for the name"
        : hit.kind === "soon" ? (hit.m.w === "Date TBA" ? "Coming soon · date TBA" : "Coming " + hit.m.w)
        : isHere(name) ? "Showing at " + hereLabel() : "Showing in the city";
      return '<span class="bt-mp-chip">' + poster(hit && hit.m, name) +
        '<span class="bt-mp-chip-text"><b>' + esc(name) + "</b><small>" + esc(meta) + "</small></span>" +
        '<button type="button" class="bt-mp-remove" data-mp-remove="' + esc(name) + '" aria-label="Remove ' +
        esc(name) + '" title="Remove">' + svg(ICON.x) + "</button></span>";
    }).join("");
    const isOpen = r.classList.contains("is-open");
    const label = list.length ? "Add another" : "Choose a movie";
    box.innerHTML = chips + '<button type="button" class="bt-mp-add' + (list.length ? "" : " is-empty") +
      '" aria-expanded="' + isOpen + '">' + svg(list.length ? ICON.plus : ICON.film) + "<span>" + label + "</span></button>";
  };

  // --- The panel of posters ----------------------------------------------------
  const items = () => {
    const c = S.cat;
    if (!c) return [];
    const q = low(S.q.trim());
    let out;
    if (q) {
      out = c.now.map((m) => ({ m, kind: "now" })).concat(c.soon.map((m) => ({ m, kind: "soon" })))
        .filter((x) => low(x.m.n).includes(q));
    } else if (S.tab === "now") {
      out = c.now.map((m) => ({ m, kind: "now" }));
    } else {
      out = c.soon.map((m) => ({ m, kind: "soon" }));
    }
    out.forEach((x) => { x.here = x.kind === "now" && isHere(x.m.n); });
    return out.sort((a, b) => (b.here ? 1 : 0) - (a.here ? 1 : 0));  // this cinema's first, else as sent
  };
  const card = (x) => {
    const m = x.m;
    const on = isPicked(m.n);
    const tag = x.here ? '<span class="bt-mp-tag here">At ' + hereLabel() + "</span>"
      : x.kind === "soon" ? '<span class="bt-mp-tag">' + esc(m.w) + "</span>" : "";
    const meta = x.kind === "soon" ? (m.l || []).slice(0, 2).join(", ")
      : [(m.l || []).slice(0, 2).join(", "), m.c].filter(Boolean).join(" · ");
    const title = m.n + ((m.g || []).length ? " · " + m.g.join(", ") : "");
    return '<button type="button" class="bt-mp-card' + (on ? " is-on" : "") + '" role="option" aria-selected="' + on +
      '" data-mp-name="' + esc(m.n) + '" title="' + esc(title) + '">' +
      poster(m, m.n, tag + '<span class="bt-mp-check">' + svg(ICON.check) + "</span>") +
      '<span class="bt-mp-name">' + esc(m.n) + '</span><span class="bt-mp-meta">' + esc(meta) + "</span></button>";
  };
  const renderPanel = (r) => {
    const panel = r && r.querySelector(".bt-mp-panel");
    if (!panel || panel.hidden) return;
    const c = S.cat;
    const searching = !!S.q.trim();
    r.querySelectorAll("[data-mp-tab]").forEach((b) => {
      const on = !searching && b.getAttribute("data-mp-tab") === S.tab;
      b.setAttribute("aria-selected", String(on));
      b.classList.toggle("is-on", on);
      b.querySelector("b").textContent = c ? String(c[b.getAttribute("data-mp-tab")].length) : "";
    });
    const all = items();
    // Language chips: the ones in this list, in the city's order (its own languages first).
    const present = new Set([].concat(...all.map((x) => x.m.l || [])));
    const langs = c ? c.langs.filter((l) => present.has(l)) : [];
    if (S.lang !== "all" && !present.has(S.lang)) S.lang = "all";
    // While searching, every language counts, so the language chips step aside.
    r.querySelector(".bt-mp-langs").innerHTML = searching || langs.length < 2 ? "" : ["all"].concat(langs).map((l) =>
      '<button type="button" data-mp-lang="' + esc(l) + '" class="' + (S.lang === l ? "is-on" : "") +
      '" aria-pressed="' + (S.lang === l) + '">' + esc(l === "all" ? "All languages" : l) + "</button>").join("");
    const shown = searching || S.lang === "all" ? all : all.filter((x) => (x.m.l || []).includes(S.lang));
    const grid = r.querySelector(".bt-mp-grid");
    if (!c) {
      grid.innerHTML = '<div class="bt-mp-empty">Loading movies…</div>';
    } else if (!shown.length) {
      grid.innerHTML = '<div class="bt-mp-empty">' + (searching
        ? "No movie called “" + esc(S.q.trim()) + "” on District yet. You can still watch for the name."
        : S.tab === "now" ? "Nothing is showing here right now." : "Nothing has been announced yet.") + "</div>";
    } else {
      grid.innerHTML = shown.map(card).join("");
    }
    // A name that isn't listed yet: watch for it anyway.
    const q = S.q.trim();
    const custom = r.querySelector(".bt-mp-custom");
    const exact = q && all.some((x) => low(x.m.n) === low(q));
    custom.hidden = !q || exact || isPicked(q);
    custom.textContent = q ? "Watch for “" + q + "” anyway" : "";
  };
  const render = () => {
    const r = root();
    if (!r || !field()) return false;
    renderPicked(r);
    renderPanel(r);
    return true;
  };

  const open = (r) => {
    if (S.cat && !S.cat.now.length && S.cat.soon.length) S.tab = "soon";
    r.querySelector(".bt-mp-panel").hidden = false;
    r.classList.add("is-open");
    render();
    const input = r.querySelector(".bt-mp-search input");
    if (input && window.matchMedia("(pointer: fine)").matches) input.focus({ preventScroll: true });
  };
  const close = (r, refocus) => {
    const panel = r.querySelector(".bt-mp-panel");
    if (!panel || panel.hidden) return;
    panel.hidden = true;
    r.classList.remove("is-open");
    renderPicked(r);
    if (refocus) { const add = r.querySelector(".bt-mp-add"); if (add) add.focus(); }
  };
  const toggle = (name) => {
    const list = picks();
    const i = list.findIndex((p) => low(p) === low(name));
    if (i >= 0) list.splice(i, 1); else list.push(name);
    save();
  };
  const clearSearch = (r) => {
    S.q = "";
    const input = r.querySelector(".bt-mp-search input");
    if (input) input.value = "";
  };

  document.addEventListener("click", (e) => {
    const t = e.target instanceof Element ? e.target : null;
    const r = root();
    if (!t || !r) return;
    if (!r.contains(t) || !t.isConnected) { if (t.isConnected) close(r, false); return; }
    if (t.closest(".bt-mp-add")) { if (r.classList.contains("is-open")) close(r, false); else open(r); return; }
    const rm = t.closest("[data-mp-remove]");
    if (rm) { toggle(rm.getAttribute("data-mp-remove")); render(); return; }
    const cardEl = t.closest(".bt-mp-card");
    if (cardEl) {
      const name = cardEl.getAttribute("data-mp-name");
      toggle(name);
      const on = isPicked(name);
      cardEl.classList.toggle("is-on", on);  // in place, so the grid keeps its scroll position
      cardEl.setAttribute("aria-selected", String(on));
      renderPicked(r);
      return;
    }
    const tab = t.closest("[data-mp-tab]");
    if (tab) {
      S.tab = tab.getAttribute("data-mp-tab");
      clearSearch(r);
      renderPanel(r);
      r.querySelector(".bt-mp-grid").scrollTop = 0;
      return;
    }
    const lang = t.closest("[data-mp-lang]");
    if (lang) {
      S.lang = lang.getAttribute("data-mp-lang");
      renderPanel(r);
      r.querySelector(".bt-mp-grid").scrollTop = 0;
      return;
    }
    if (t.closest(".bt-mp-custom")) {
      const q = S.q.trim();
      if (q && !isPicked(q)) toggle(q);
      clearSearch(r);
      render();
      return;
    }
    if (t.closest(".bt-mp-done")) close(r, true);
  });
  document.addEventListener("input", (e) => {
    const input = e.target instanceof Element ? e.target.closest(".bt-mp .bt-mp-search input") : null;
    if (!input) return;
    S.q = input.value;
    const r = root();
    renderPanel(r);
    r.querySelector(".bt-mp-grid").scrollTop = 0;
  });
  document.addEventListener("keydown", (e) => {
    const r = root();
    if (!r || !r.classList.contains("is-open")) return;
    if (e.key === "Escape") { e.preventDefault(); close(r, true); return; }
    const input = e.target instanceof Element ? e.target.closest(".bt-mp .bt-mp-search input") : null;
    if (input && e.key === "Enter") {
      e.preventDefault();
      const shown = r.querySelectorAll(".bt-mp-card");
      const custom = r.querySelector(".bt-mp-custom");
      if (shown.length === 1) shown[0].click();
      else if (custom && !custom.hidden) custom.click();
    }
  });
  document.addEventListener("focusin", (e) => {
    const r = root();
    if (r && r.classList.contains("is-open") && e.target instanceof Element && !r.contains(e.target)) close(r, false);
  });

  BT.movies = {
    refresh: () => { render(); return []; },
    picked: () => picks().slice(),
    load: (json) => {
      try { S.cat = JSON.parse(json || "null"); } catch (e) { S.cat = null; }
      if (S.cat) ["here", "now", "soon", "langs"].forEach((k) => { if (!Array.isArray(S.cat[k])) S.cat[k] = []; });
      if (!render()) setTimeout(() => BT.movies.load(json), 200);
      return [];
    },
  };
  // Draw the picks as soon as the field exists, before the posters arrive.
  const wait = setInterval(() => { if (render()) clearInterval(wait); }, 200);
  setTimeout(() => clearInterval(wait), 30000);
})();

// ---------------------------------------------------------------------------
// Cinema picker. Python sends the city's cinemas as JSON (#bt-cinema-catalog):
// {city, label, cinemas: [{u: link, n: name, a: area, m: [movies showing]}]}. Picks go
// into a hidden textbox (#bt-cinema-value) as a JSON list of links, with "city:<city>" first
// for anywhere in the city (cinemas picked as well get their own alert as each opens).
// A pasted link arrives in #bt-cinema-add.
// ---------------------------------------------------------------------------
(() => {
  const BT = window.BookTick;
  if (!BT || BT.cinemas) return;
  const CITY = "city:";
  const S = { cat: null, picks: null, q: "", names: {} };
  const root = () => document.querySelector(".bt-cp");
  const field = () => document.querySelector("#bt-cinema-value input, #bt-cinema-value textarea");
  const max = () => Number((root() || {}).dataset ? root().dataset.max : 5) || 5;
  const esc = (t) => String(t == null ? "" : t).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const svg = (d) => '<svg class="bt-i" viewBox="0 0 24 24" aria-hidden="true">' + d + "</svg>";
  const ICON = {
    pin: '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
    city: '<path d="M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18Z"/><path d="M6 12H4a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2"/><path d="M18 9h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-2"/><path d="M10 6h4M10 10h4M10 14h4M10 18h4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>', x: '<path d="M18 6 6 18M6 6l12 12"/>', check: '<path d="M20 6 9 17l-5-5"/>',
  };
  const low = (s) => String(s).toLocaleLowerCase();
  const cid = (u) => (String(u).match(/-CD(\d+)/i) || [])[1] || String(u);
  const slugName = (u) => {
    const slug = String(u).split("?")[0].replace(/\/+$/, "").split("/").pop().replace(/-CD\d+$/i, "");
    return slug.split("-").filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" ") || "Cinema";
  };

  const picks = () => {
    if (S.picks === null) {
      const el = field();
      if (!el) return [];
      try {
        const v = JSON.parse(el.value || "[]");
        S.picks = Array.isArray(v) ? v.map(String).filter(Boolean) : [];
      } catch (e) {
        S.picks = [];
      }
    }
    return S.picks;
  };
  const save = () => {
    const el = field();
    if (!el) return;
    el.value = JSON.stringify(picks());
    el.dispatchEvent(new Event("input", { bubbles: true }));  // tells Gradio the value changed
    if (BT.movies && BT.movies.refresh) BT.movies.refresh();  // "At your cinema" follows these picks
  };
  const anywhere = () => picks().some((p) => p.startsWith(CITY));
  const picked = () => picks().filter((p) => !p.startsWith(CITY)).length;  // cinemas only
  const cinemaOf = (u) => (S.cat ? S.cat.cinemas.find((c) => cid(c.u) === cid(u)) : null);
  const nameOf = (u) => { const c = cinemaOf(u); return c ? c.n : S.names[cid(u)] || slugName(u); };
  const isPicked = (u) => picks().some((p) => !p.startsWith(CITY) && cid(p) === cid(u));
  const movies = () => (BT.movies && BT.movies.picked ? BT.movies.picked() : []);
  // A cinema showing one of the movies picked, by name (part of a name counts, as in watchers).
  const showing = (c) => {
    const wanted = movies().map(low);
    return (c.m || []).find((m) => wanted.some((w) => low(m).includes(w) || w.includes(low(m)))) || null;
  };

  // --- The picks, as chips ------------------------------------------------------
  const renderPicked = (r) => {
    const list = picks();
    const label = S.cat ? S.cat.label : "the city";
    const chip = (icon, title, sub, value) => '<span class="bt-mp-chip bt-cp-chip"><span class="bt-cp-icon">' + svg(icon) +
      '</span><span class="bt-mp-chip-text"><b>' + esc(title) + "</b><small>" + esc(sub) + "</small></span>" +
      '<button type="button" class="bt-mp-remove" data-cp-remove="' + esc(value) + '" aria-label="Remove ' + esc(title) +
      '" title="Remove">' + svg(ICON.x) + "</button></span>";
    const chips = list.map((p) => {
      if (p.startsWith(CITY)) return chip(ICON.city, "Anywhere in " + label, "Every cinema in the city", p);
      const c = cinemaOf(p);
      const sub = c ? (c.a || "In " + label) : "Pasted link";
      return chip(ICON.pin, nameOf(p), sub, p);
    }).join("");
    const full = picked() >= max();
    const isOpen = r.classList.contains("is-open");
    const text = !list.length ? "Choose where" : full ? "Change cinemas" : "Add a cinema";
    r.querySelector(".bt-cp-picked").innerHTML = chips + '<button type="button" class="bt-mp-add bt-cp-add' +
      (list.length ? "" : " is-empty") + '" aria-expanded="' + isOpen + '">' + svg(list.length ? ICON.plus : ICON.pin) +
      "<span>" + text + "</span></button>";
  };

  // --- The panel: anywhere in the city, then each cinema --------------------------
  const renderPanel = (r) => {
    const panel = r && r.querySelector(".bt-cp-panel");
    if (!panel || panel.hidden) return;
    const label = S.cat ? S.cat.label : "the city";
    const any = anywhere();
    const anyBtn = r.querySelector(".bt-cp-anywhere");
    anyBtn.classList.toggle("is-on", any);
    anyBtn.setAttribute("aria-pressed", String(any));
    anyBtn.innerHTML = '<span class="bt-cp-icon">' + svg(ICON.city) + '</span><span class="bt-cp-text"><b>Anywhere in ' +
      esc(label) + "</b><small>" + (any ? "Every cinema in the city. Tick cinemas below to hear about each of them too."
        : "Every cinema in the city. You hear wherever it opens first.") + "</small></span>" +
      '<span class="bt-cp-check">' + svg(ICON.check) + "</span>";
    const list = r.querySelector(".bt-cp-list");
    if (!S.cat) {
      list.innerHTML = '<div class="bt-mp-empty">Loading cinemas…</div>';
    } else {
      const q = low(S.q.trim());
      const rows = S.cat.cinemas.filter((c) => !q || low(c.n + " " + c.a).includes(q))
        .map((c) => ({ c, hit: showing(c) }))
        .sort((x, y) => (y.hit ? 1 : 0) - (x.hit ? 1 : 0));  // ones showing your movie first, else A–Z as sent
      const full = picked() >= max();
      list.innerHTML = rows.length ? rows.map(({ c, hit }) => {
        const on = isPicked(c.u);
        const off = !on && full;
        const badge = hit ? '<span class="bt-cp-badge hot">Showing ' + esc(hit) + "</span>"
          : '<span class="bt-cp-badge">' + (c.m || []).length + " movies</span>";
        return '<button type="button" class="bt-cp-row' + (on ? " is-on" : "") + (off ? " is-full" : "") +
          '" role="option" aria-selected="' + on + '" data-cp-cinema="' + esc(c.u) + '"' + (off ? ' title="Up to ' +
          max() + ' cinemas"' : "") + '><span class="bt-cp-check">' + svg(ICON.check) + '</span><span class="bt-cp-text"><b>' +
          esc(c.n) + "</b><small>" + esc(c.a || label) + (hit ? '<em class="bt-cp-hit"> · Showing ' + esc(hit) + "</em>" : "") +
          "</small></span>" + badge + "</button>";
      }).join("") : '<div class="bt-mp-empty">No cinema called “' + esc(S.q.trim()) + "” in " + esc(label) +
        ". You can paste its link from district.in below.</div>";
    }
    const n = picked();
    const cinemas = n + (n === 1 ? " cinema" : " cinemas");
    r.querySelector(".bt-cp-count").textContent = any ? "Whole city" + (n ? " + " + cinemas : "")
      : n ? n + " of " + max() + " cinemas picked" : "Pick up to " + max() + " cinemas";
  };
  const render = () => {
    const r = root();
    if (!r || !field()) return false;
    renderPicked(r);
    renderPanel(r);
    return true;
  };

  const open = (r) => {
    r.querySelector(".bt-cp-panel").hidden = false;
    r.classList.add("is-open");
    render();
    const input = r.querySelector(".bt-mp-search input");
    if (input && window.matchMedia("(pointer: fine)").matches) input.focus({ preventScroll: true });
  };
  const close = (r, refocus) => {
    const panel = r.querySelector(".bt-cp-panel");
    if (!panel || panel.hidden) return;
    panel.hidden = true;
    r.classList.remove("is-open");
    renderPicked(r);
    if (refocus) { const add = r.querySelector(".bt-cp-add"); if (add) add.focus(); }
  };
  const toggleCinema = (u) => {
    const list = picks();
    const i = list.findIndex((p) => !p.startsWith(CITY) && cid(p) === cid(u));
    if (i >= 0) list.splice(i, 1);
    else if (picked() < max()) list.push(u);
    else return false;
    save();
    return true;
  };

  document.addEventListener("click", (e) => {
    const t = e.target instanceof Element ? e.target : null;
    const r = root();
    if (!t || !r || !t.isConnected) return;
    if (!r.contains(t)) { close(r, false); return; }
    if (t.closest(".bt-cp-add")) { if (r.classList.contains("is-open")) close(r, false); else open(r); return; }
    const rm = t.closest("[data-cp-remove]");
    if (rm) {
      const v = rm.getAttribute("data-cp-remove");
      S.picks = picks().filter((p) => p !== v);
      save();
      render();
      return;
    }
    if (t.closest(".bt-cp-anywhere")) {
      // On or off; the cinemas picked stay either way.
      const cinemas = picks().filter((p) => !p.startsWith(CITY));
      S.picks = anywhere() || !S.cat ? cinemas : [CITY + S.cat.city].concat(cinemas);
      save();
      render();
      return;
    }
    const row = t.closest("[data-cp-cinema]");
    if (row) {
      if (toggleCinema(row.getAttribute("data-cp-cinema"))) render();
      return;
    }
    if (t.closest(".bt-cp-done")) close(r, true);
  });
  document.addEventListener("input", (e) => {
    const input = e.target instanceof Element ? e.target.closest(".bt-cp .bt-mp-search input") : null;
    if (!input) return;
    S.q = input.value;
    renderPanel(root());
    root().querySelector(".bt-cp-list").scrollTop = 0;
  });
  document.addEventListener("keydown", (e) => {
    const r = root();
    if (!r || !r.classList.contains("is-open")) return;
    if (e.key === "Escape") { e.preventDefault(); close(r, true); return; }
    const input = e.target instanceof Element ? e.target.closest(".bt-cp .bt-mp-search input") : null;
    if (input && e.key === "Enter") {
      e.preventDefault();
      const rows = r.querySelectorAll(".bt-cp-row:not(.is-full)");
      if (rows.length === 1) rows[0].click();
    }
  });
  document.addEventListener("focusin", (e) => {
    const r = root();
    if (r && r.classList.contains("is-open") && e.target instanceof Element && !r.contains(e.target)) close(r, false);
  });

  BT.cinemas = {
    load: (json) => {
      let next = null;
      try { next = JSON.parse(json || "null"); } catch (e) { next = null; }
      if (next && !Array.isArray(next.cinemas)) next.cinemas = [];
      // A different city: the cinemas picked in the last one don't carry over.
      if (next && S.cat && S.cat.city !== next.city && picks().length) { S.picks = []; save(); }
      S.cat = next;
      if (next && BT.formats) BT.formats.choices(next.formats);
      if (!render()) setTimeout(() => BT.cinemas.load(json), 200);
      if (BT.movies && BT.movies.refresh) BT.movies.refresh();
      return [];
    },
    // A pasted link (from Python): add it to the picks.
    add: (json) => {
      let c = null;
      try { c = JSON.parse(json || "null"); } catch (e) { c = null; }
      if (!c || !c.u) return [];
      S.names[cid(c.u)] = c.n;
      if (!isPicked(c.u) && picked() < max()) { picks().push(c.u); save(); }
      render();
      return [];
    },
    refresh: () => { render(); return []; },
    // For the movie picker: the movies showing at the cinemas picked (null for anywhere or none).
    movieNames: () => {
      if (!S.cat || anywhere()) return null;
      const list = picks();
      if (!list.length) return null;
      const names = new Set();
      list.forEach((p) => { const c = cinemaOf(p); (c ? c.m || [] : []).forEach((m) => names.add(low(m))); });
      return names;
    },
    count: () => (anywhere() ? 0 : picks().length),
  };
  const wait = setInterval(() => { if (render()) clearInterval(wait); }, 200);
  setTimeout(() => clearInterval(wait), 30000);
})();

// ---------------------------------------------------------------------------
// Format chips. "Any format" is the default; ticking a format turns it off, and clearing
// them all turns it back on. The city's own screen features arrive with its cinemas
// (BookTick.cinemas.load). Picks go into #bt-formats-value as a JSON list ([] = any).
// ---------------------------------------------------------------------------
(() => {
  const BT = window.BookTick;
  if (!BT || BT.formats) return;
  const S = { choices: null, picks: null, typing: false };
  const root = () => document.querySelector(".bt-fmt");
  const field = () => document.querySelector("#bt-formats-value input, #bt-formats-value textarea");
  const esc = (t) => String(t == null ? "" : t).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const svg = (d) => '<svg class="bt-i" viewBox="0 0 24 24" aria-hidden="true">' + d + "</svg>";
  const CHECK = '<path d="M20 6 9 17l-5-5"/>', PLUS = '<path d="M12 5v14M5 12h14"/>';
  const low = (s) => String(s).toLowerCase();

  const choices = () => {
    if (S.choices === null) {
      try { S.choices = JSON.parse((root() || {}).dataset ? root().dataset.choices || "[]" : "[]"); } catch (e) { S.choices = []; }
    }
    return S.choices;
  };
  const picks = () => {
    if (S.picks === null) {
      const el = field();
      if (!el) return [];
      try { const v = JSON.parse(el.value || "[]"); S.picks = Array.isArray(v) ? v.map(String).filter(Boolean) : []; }
      catch (e) { S.picks = []; }
    }
    return S.picks;
  };
  const save = () => {
    const el = field();
    if (!el) return;
    el.value = JSON.stringify(picks());
    el.dispatchEvent(new Event("input", { bubbles: true }));  // tells Gradio the value changed
  };
  const render = () => {
    const r = root();
    if (!r || !field()) return false;
    const list = picks();
    const known = choices();
    const extra = list.filter((p) => !known.some((k) => low(k) === low(p)));  // typed in, or from another city
    const any = !list.length;
    const chip = (label) => {
      const on = list.some((p) => low(p) === low(label));
      return '<button type="button" class="bt-fmt-chip' + (on ? " is-on" : "") + '" data-fmt="' + esc(label) +
        '" aria-pressed="' + on + '">' + esc(label) + "</button>";
    };
    const other = S.typing
      ? '<span class="bt-fmt-chip bt-fmt-input"><input type="text" maxlength="30" placeholder="e.g. Recliner" aria-label="Another format or screen"></span>'
      : '<button type="button" class="bt-fmt-chip bt-fmt-other" data-fmt-other>' + svg(PLUS) + "Other</button>";
    r.querySelector(".bt-fmt-chips").innerHTML =
      '<button type="button" class="bt-fmt-chip bt-fmt-any' + (any ? " is-on" : "") + '" data-fmt-any aria-pressed="' + any +
      '">' + (any ? svg(CHECK) : "") + "Any format</button>" + known.concat(extra).map(chip).join("") + other;
    if (S.typing) { const input = r.querySelector(".bt-fmt-input input"); if (input) input.focus(); }
    return true;
  };
  const toggle = (label) => {
    const list = picks();
    const i = list.findIndex((p) => low(p) === low(label));
    if (i >= 0) list.splice(i, 1); else list.push(label);
    save();
  };
  const addTyped = (input) => {
    const text = input.value.replace(/\s+/g, " ").trim();
    S.typing = false;
    if (text && !picks().some((p) => low(p) === low(text))) { picks().push(text); save(); }
    render();
  };

  document.addEventListener("click", (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (!t || !t.closest(".bt-fmt")) return;
    if (t.closest("[data-fmt-any]")) { S.picks = []; save(); render(); return; }
    if (t.closest("[data-fmt-other]")) { S.typing = true; render(); return; }
    const chip = t.closest("[data-fmt]");
    if (chip) { toggle(chip.getAttribute("data-fmt")); render(); }
  });
  document.addEventListener("keydown", (e) => {
    const input = e.target instanceof Element ? e.target.closest(".bt-fmt-input input") : null;
    if (!input) return;
    if (e.key === "Enter") { e.preventDefault(); addTyped(input); }
    if (e.key === "Escape") { e.preventDefault(); S.typing = false; render(); }
  });
  document.addEventListener("focusout", (e) => {
    const input = e.target instanceof Element ? e.target.closest(".bt-fmt-input input") : null;
    if (input) setTimeout(() => { if (S.typing) addTyped(input); }, 120);
  });

  BT.formats = {
    // The chips for the city picked: the usual ones, then its cinemas' screen features.
    choices: (list) => { if (Array.isArray(list) && list.length) { S.choices = list.map(String); render(); } return []; },
  };
  const wait = setInterval(() => { if (render()) clearInterval(wait); }, 200);
  setTimeout(() => clearInterval(wait), 30000);
})();

// ---------------------------------------------------------------------------
// Your name and ntfy topic: remembered in this browser, sent with BookTick.identity() so the
// next visit starts with them, and kept the same in the watcher and tracker forms.
// ---------------------------------------------------------------------------
(() => {
  const BT = window.BookTick;
  if (!BT || BT.remember) return;
  const FIELDS = { "bt-name": "name", "bt-tr-name": "name", "bt-ntfy": "ntfy", "bt-tr-ntfy": "ntfy" };
  const KEYS = { name: "booktick-name", ntfy: "booktick-ntfy" };
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); } catch (e) {} },
  };
  BT.remember = { get: (what) => store.get(KEYS[what]) || "" };
  document.addEventListener("change", (e) => {
    const input = e.target instanceof Element ? e.target.closest("input, textarea") : null;
    const box = input && input.closest("[id]");
    const what = box && FIELDS[box.id];
    if (!what) return;
    const value = input.value.trim();
    store.set(KEYS[what], value);
    // The same value in the other form, so both always match.
    Object.keys(FIELDS).filter((id) => FIELDS[id] === what && id !== box.id).forEach((id) => {
      const other = document.querySelector("#" + id + " input, #" + id + " textarea");
      if (other && other.value !== value) {
        other.value = value;
        other.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });
  });
})();
