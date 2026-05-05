// Concinno Config GUI — enterprise single-page frontend.
//
// Design principles:
//   - English only (browser translate handles the rest)
//   - Deterministic sort (shared SORT_KEY with Python feature_readme)
//   - No save button — change → POST → status stamp
//   - Live auto-refresh via 3s digest poll so LLM-side edits propagate
//   - Per-feature ? tooltip with plain-English explanation + example
//   - Two-layer ZIQ control:
//       * feature-level ziq_opt_out toggle (header)
//       * param-level manual_pinned 🔒 (each param row)
//   - Clickable facet badges + active-chip bar
//
(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const status = $("#status");

  function setStatus(msg, kind = "info") {
    status.textContent = msg;
    status.className = `status ${kind}`;
  }

  // Bearer token from URL ?bearer=<token> (set by switcher / VS Code
  // extension when embedding this SPA in iframe webview). Loopback-only
  // surface; URL param is acceptable per spec for personal CLI tooling.
  // Empty token = direct browser access (also loopback-only); /api/*
  // BearerTokenMiddleware will 401 unless user adds the token manually.
  const _BEARER = new URLSearchParams(window.location.search).get("bearer") || "";

  async function fetchJSON(url, opts = {}) {
    const headers = {
      "content-type": "application/json",
      ...(opts.headers || {}),
    };
    if (_BEARER) headers["Authorization"] = `Bearer ${_BEARER}`;
    const r = await fetch(url, { ...opts, headers });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${url}`);
    return r.json();
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ── Shared typeahead widget — Google-style suggestion dropdown ──
  // Any filter input can be wrapped; the ``suggest(q)`` callback returns
  // ``[{value, label}]`` rows. Keyboard ↑↓/Enter/Esc + mouse supported.
  function wireTypeahead(input, suggest) {
    if (!input || input.dataset.typeahead === "1") return;
    input.dataset.typeahead = "1";
    input.setAttribute("autocomplete", "off");
    const wrap = document.createElement("span");
    wrap.className = "ta-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const box = document.createElement("ul");
    box.className = "typeahead-box";
    box.style.display = "none";
    wrap.appendChild(box);
    let activeIdx = -1;

    function render() {
      const q = input.value.toLowerCase().trim();
      const items = q ? suggest(q).slice(0, 8) : suggest("").slice(0, 8);
      if (!items.length) { box.style.display = "none"; return; }
      box.innerHTML = items.map((it, i) =>
        `<li data-val="${escapeHtml(it.value)}" class="${i === activeIdx ? "active" : ""}">
          <strong translate="no">${escapeHtml(it.value)}</strong>
          <span class="ta-desc">${escapeHtml(it.label || "")}</span>
        </li>`,
      ).join("");
      box.style.display = "block";
      box.querySelectorAll("li").forEach((li) => {
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          input.value = li.dataset.val;
          box.style.display = "none";
          input.dispatchEvent(new Event("input", { bubbles: true }));
        });
      });
    }

    input.addEventListener("input", () => { activeIdx = -1; render(); });
    input.addEventListener("focus", render);
    input.addEventListener("blur", () => setTimeout(() => { box.style.display = "none"; }, 180));
    input.addEventListener("keydown", (e) => {
      if (box.style.display === "none") return;
      const lis = box.querySelectorAll("li");
      if (!lis.length) return;
      if (e.key === "ArrowDown") {
        e.preventDefault(); activeIdx = Math.min(activeIdx + 1, lis.length - 1); render();
      } else if (e.key === "ArrowUp") {
        e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); render();
      } else if (e.key === "Enter" && activeIdx >= 0) {
        e.preventDefault();
        const li = lis[activeIdx];
        input.value = li.dataset.val;
        box.style.display = "none";
        input.dispatchEvent(new Event("input", { bubbles: true }));
      } else if (e.key === "Escape") {
        box.style.display = "none"; activeIdx = -1;
      }
    });
  }

  function _desc(s) { return (s || "").slice(0, 80); }

  // Wire typeahead to every filter input as soon as DOM is parsed.
  setTimeout(() => {
    wireTypeahead($("#filter"), (q) => featuresCache
      .filter((f) => !q || `${f.name} ${f.description || ""}`.toLowerCase().includes(q))
      .map((f) => ({ value: f.name, label: _desc(f.description) })));
    wireTypeahead($("#skills-filter"), (q) => skillsCache
      .filter((s) => !q || `${s.name} ${s.description || ""}`.toLowerCase().includes(q))
      .map((s) => ({ value: s.name, label: _desc(s.description) })));
    wireTypeahead($("#commands-filter"), (q) => commandsCache
      .filter((c) => !q || `${c.slug} ${c.description || ""}`.toLowerCase().includes(q.replace(/^\//, "")))
      .map((c) => ({ value: `/${c.slug}`, label: _desc(c.description) })));
    wireTypeahead($("#harness-filter"), (q) => {
      const rules = [];
      for (const f of (harnessCache || [])) {
        for (const k of ["allow", "deny", "ask"]) {
          for (const r of (f.permissions || {})[k] || []) rules.push({ value: r, label: `[${k}] ${f.path.split(/[/\\]/).pop()}` });
        }
      }
      return q ? rules.filter((r) => r.value.toLowerCase().includes(q)) : rules.slice(0, 8);
    });
  }, 50);

  // ── Tabs ──────────────────────────────────────────────
  $$("nav.tabs button[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$("nav.tabs button[data-tab]").forEach((b) => b.classList.remove("active"));
      $$("section.tab").forEach((s) => s.classList.remove("active"));
      btn.classList.add("active");
      $(`#tab-${btn.dataset.tab}`).classList.add("active");
      loadTab(btn.dataset.tab);
    });
  });

  // ── Facet state ──────────────────────────────────────
  const facets = { category: null, flag: null, effect: null };

  function renderFacetChips() {
    const host = $("#active-facets");
    host.innerHTML = "";
    const active = [
      facets.category && { k: "category", v: facets.category, label: `category = ${facets.category}` },
      facets.flag && { k: "flag", v: facets.flag, label: `flag = ${facets.flag}` },
      facets.effect && { k: "effect", v: facets.effect, label: `effect = ${facets.effect}` },
    ].filter(Boolean);
    for (const a of active) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.innerHTML = `${escapeHtml(a.label)} <button type="button" aria-label="remove">×</button>`;
      chip.querySelector("button").addEventListener("click", () => {
        facets[a.k] = null;
        renderFeatures();
      });
      host.appendChild(chip);
    }
  }

  // ── Features list ────────────────────────────────────
  let featuresCache = [];
  let lastDigest = null;

  const SORT_FNS = {
    "category-name": (a, b) =>
      (a.category || "zz").localeCompare(b.category || "zz") ||
      a.name.localeCompare(b.name),
    "name": (a, b) => a.name.localeCompare(b.name),
    "non-default-first": (a, b) => nonDefaultScore(b) - nonDefaultScore(a) || a.name.localeCompare(b.name),
    "ziq-first": (a, b) => (b.ziq_effective ? 1 : 0) - (a.ziq_effective ? 1 : 0) || a.name.localeCompare(b.name),
    "effect-scope": (a, b) => {
      const order = { immediate: 0, process_restart: 1, session_restart: 2 };
      return (order[a.effect_scope] ?? 9) - (order[b.effect_scope] ?? 9) || a.name.localeCompare(b.name);
    },
  };

  // Source-badge builder shared across feature / skill cards.
  // Returns an HTML span for any row whose `source` (or `scope`)
  // indicates a user / plugin / merged origin. Empty string when the
  // row is the default `official` / `user-default` case.
  //
  // Added in the 2.33.1 handoff §3.1 follow-up: both feature and
  // skill cards had grown three-way branching for plugin badge
  // rendering inline; consolidating here drops ~8 lines of duplicate
  // HTML string logic and keeps future source-kind additions
  // (e.g. `remote:<url>`) in one place.
  function renderSourceBadge(source) {
    if (!source || typeof source !== "string") return "";
    if (source === "user") {
      return '<span class="badge source-user" title="User-registered feature from ~/.concinno/user_features.json">user</span>';
    }
    if (source.startsWith("plugin:")) {
      const pkg = source.slice(7);
      return `<span class="badge source-plugin" title="From installed package ${escapeHtml(pkg)}">${escapeHtml(source)}</span>`;
    }
    if (source.startsWith("merged:")) {
      const sources = source.slice(7);
      return `<span class="badge source-merged" title="Merged from multiple sources: ${escapeHtml(sources)}">merged</span>`;
    }
    return "";
  }

  function nonDefaultScore(f) {
    let s = f.enabled === false ? 2 : 0;
    for (const p of Object.values(f.params || {})) if (p.is_modified) s += 1;
    if (f.ziq_opt_out) s += 1;
    return s;
  }

  function renderFeatures() {
    const filter = $("#filter").value.toLowerCase().trim();
    const onlyNonDefault = $("#only-non-default").checked;
    const sortKey = $("#sort").value;
    renderFacetChips();
    const list = $("#features-list");
    list.innerHTML = "";
    const filtered = featuresCache.filter((f) => {
      if (facets.category && f.category !== facets.category) return false;
      if (facets.flag === "ziq" && !f.ziq_autotunable) return false;
      if (facets.flag === "cosmetic" && !f.cosmetic) return false;
      if (facets.effect && f.effect_scope !== facets.effect) return false;
      if (filter) {
        const hay = `${f.name} ${f.category} ${f.description || ""} ${f.example || ""}`.toLowerCase();
        if (!hay.includes(filter)) return false;
      }
      if (onlyNonDefault && nonDefaultScore(f) === 0) return false;
      return true;
    });
    filtered.sort(SORT_FNS[sortKey] || SORT_FNS["category-name"]);
    for (const f of filtered) list.appendChild(renderFeatureCard(f));
    setStatus(`${filtered.length} / ${featuresCache.length} features`);
  }

  function renderFeatureCard(f) {
    const card = document.createElement("div");
    card.className = "feature";
    card.dataset.name = f.name;
    const scope = f.effect_scope || "immediate";
    const ziqToggle = f.ziq_autotunable
      ? `<label class="ziq-toggle" title="When ON, ZIQ may auto-tune this feature's params (skipping ones you pinned 🔒). When OFF, the whole feature is opaque to ZIQ.">
           <input type="checkbox" data-feature="${f.name}" data-key="ziq_opt_out" data-invert="1" ${f.ziq_opt_out ? "" : "checked"}>
           <span>ZIQ</span>
         </label>`
      : "";
    const exampleBlock = f.example
      ? `<div class="help-tooltip">${escapeHtml(f.example)}</div>`
      : `<div class="help-tooltip">${escapeHtml(f.description || "No extended description.")}</div>`;
    card.innerHTML = `
      <header class="fhead">
        <label class="toggle">
          <input type="checkbox" data-feature="${f.name}" data-key="enabled" ${f.enabled ? "checked" : ""}>
          <code>${f.name}</code>
        </label>
        <span class="badge cat" data-facet="category" data-val="${escapeHtml(f.category || "")}">${escapeHtml(f.category || "?")}</span>
        ${f.ziq_autotunable ? '<span class="badge ziq" data-facet="flag" data-val="ziq">ZIQ-tunable</span>' : ""}
        ${f.cosmetic ? '<span class="badge cosmetic" data-facet="flag" data-val="cosmetic">cosmetic</span>' : ""}
        ${renderSourceBadge(f.source)}
        <span class="badge effect-${scope.replace("_","-")}" data-facet="effect" data-val="${scope}">${scope}</span>
        ${nonDefaultScore(f) > 0 ? '<span class="badge non-default">modified</span>' : ""}
        ${ziqToggle}
        <button type="button" class="help-btn" aria-label="help">?</button>
      </header>
      <p class="desc">${escapeHtml(f.description || "")}</p>
      <div class="params"></div>
      ${exampleBlock}`;
    // ? help button → toggle .help-tooltip visibility (CSS sibling
    // selector doesn't work because the button and tooltip are in
    // different subtrees). Wire mouseenter/leave + focus/blur for
    // keyboard accessibility.
    const helpBtn = card.querySelector(".help-btn");
    const helpTip = card.querySelector(".help-tooltip");
    if (helpBtn && helpTip) {
      const show = () => helpTip.classList.add("show");
      const hide = () => helpTip.classList.remove("show");
      helpBtn.addEventListener("mouseenter", show);
      helpBtn.addEventListener("mouseleave", hide);
      helpBtn.addEventListener("focus", show);
      helpBtn.addEventListener("blur", hide);
      helpBtn.addEventListener("click", (e) => {
        e.preventDefault();
        helpTip.classList.toggle("show");
      });
      helpTip.addEventListener("mouseenter", show);
      helpTip.addEventListener("mouseleave", hide);
    }
    // Toggle (enabled)
    card.querySelector('input[data-key="enabled"]').addEventListener("change", (e) => {
      patchFeature(f.name, "enabled", e.target.checked, false);
    });
    // ZIQ toggle (writes ziq_opt_out as the *inverse* of the checked state)
    const ziqBox = card.querySelector('input[data-key="ziq_opt_out"]');
    if (ziqBox) {
      ziqBox.addEventListener("change", (e) => {
        patchFeature(f.name, "ziq_opt_out", !e.target.checked, false);
      });
    }
    // Badge facet clicks
    card.querySelectorAll(".badge[data-facet]").forEach((b) => {
      b.addEventListener("click", () => {
        const k = b.dataset.facet; const v = b.dataset.val;
        facets[k] = facets[k] === v ? null : v;
        renderFeatures();
      });
    });
    // Params
    const paramsDiv = card.querySelector(".params");
    for (const [pname, p] of Object.entries(f.params || {})) {
      paramsDiv.appendChild(renderParam(f.name, pname, p, f));
    }
    return card;
  }

  function renderParam(featureName, pname, p, feat) {
    const div = document.createElement("div");
    div.className = "param";
    const cur = (p.current === null || p.current === undefined) ? p.default : p.current;
    let input = "";
    if (p.type === "bool") {
      input = `<input type="checkbox" ${cur ? "checked" : ""} data-feature="${featureName}" data-key="${pname}" data-ptype="bool">`;
    } else if (p.options && Array.isArray(p.options)) {
      input = `<select data-feature="${featureName}" data-key="${pname}" data-ptype="str" title="${pname}">` +
        p.options.map((o) => `<option value="${escapeHtml(o)}"${o === cur ? " selected" : ""}>${escapeHtml(o)}</option>`).join("") +
        `</select>`;
    } else if (p.type === "int" || p.type === "float") {
      const step = p.type === "float" ? "any" : "1";
      const minA = (p.min !== null && p.min !== undefined) ? ` min="${p.min}"` : "";
      const maxA = (p.max !== null && p.max !== undefined) ? ` max="${p.max}"` : "";
      input = `<input type="number" step="${step}"${minA}${maxA} value="${cur ?? ""}" data-feature="${featureName}" data-key="${pname}" data-ptype="${p.type}">`;
    } else {
      input = `<input type="text" value="${escapeHtml(cur ?? "")}" data-feature="${featureName}" data-key="${pname}" data-ptype="str">`;
    }
    const meta = [
      p.default !== null && p.default !== undefined ? `default=<code>${escapeHtml(p.default)}</code>` : "",
      p.recommended !== undefined ? `rec=<code>${escapeHtml(p.recommended)}</code>` : "",
      (p.min !== null && p.min !== undefined) ? `≥${p.min}` : "",
      (p.max !== null && p.max !== undefined) ? `≤${p.max}` : "",
    ].filter(Boolean).join(" · ");
    const pinIcon = p.manual_pinned ? "🔒" : "🔄";
    const pinTitle = p.manual_pinned
      ? "Pinned: ZIQ will not override this param. Click to unpin."
      : "Unpinned: ZIQ may auto-tune this param (if feature ZIQ is on).";
    const pinButton = feat.ziq_autotunable
      ? `<button type="button" class="pin-btn" data-feature="${featureName}" data-key="${pname}" data-pinned="${p.manual_pinned ? 1 : 0}" title="${pinTitle}">${pinIcon}</button>`
      : "";
    div.innerHTML = `<label><code translate="no">${pname}</code> <small translate="no">${p.type || ""}</small></label>${input}<span class="meta" translate="no">${meta}${p.is_modified ? " · <span class='dirty'>modified</span>" : ""}</span>${pinButton}`;
    const ctrl = div.querySelector("input, select");
    const debounced = p.type === "int" || p.type === "float" || (p.type !== "bool" && (!p.options || !p.options.length));
    ctrl.addEventListener(debounced ? "input" : "change", (e) => {
      const ptype = e.target.dataset.ptype;
      let val = e.target.type === "checkbox" ? e.target.checked : e.target.value;
      if (ptype === "int") val = parseInt(val, 10);
      else if (ptype === "float") val = parseFloat(val);
      patchFeature(featureName, pname, val, debounced);
    });
    const pinBtn = div.querySelector(".pin-btn");
    if (pinBtn) {
      pinBtn.addEventListener("click", () => {
        const now = pinBtn.dataset.pinned === "1";
        patchFeature(featureName, `${pname}__pinned`, !now, false);
      });
    }
    return div;
  }

  // ── Save (debounced for text/num) ────────────────────
  const debounceTimers = new Map();

  async function postFeature(name, key, value, force) {
    return fetchJSON(`/api/features/${name}`, {
      method: "POST",
      body: JSON.stringify({ key, value, force }),
    });
  }

  async function handleRiskConfirm(name, key, value, warnings) {
    if (!$("#confirm-risky").checked) {
      const retry = await postFeature(name, key, value, true);
      setStatus(retry.applied ? "saved (forced)" : "blocked", retry.applied ? "ok" : "err");
      return;
    }
    const ok = confirm(`Risk warnings — apply anyway?\n\n${warnings.join("\n")}`);
    if (!ok) { setStatus("change declined", "warn"); return; }
    const retry = await postFeature(name, key, value, true);
    setStatus(retry.applied ? "saved (forced)" : "blocked", retry.applied ? "ok" : "err");
  }

  async function doSave(name, key, value) {
    setStatus(`saving ${name}.${key}…`);
    try {
      const res = await postFeature(name, key, value, false);
      if (!res.applied) {
        await handleRiskConfirm(name, key, value, res.warnings || []);
      } else {
        const ts = new Date().toLocaleTimeString();
        const warnSuf = (res.warnings || []).length ? " (with warnings)" : "";
        setStatus(`${name}.${key} saved${warnSuf} @ ${ts}`, "ok");
      }
      await loadFeatures(false);
    } catch (err) {
      setStatus(`Error: ${err.message}`, "err");
    }
  }

  function patchFeature(name, key, value, debounce) {
    const dkey = `${name}/${key}`;
    if (debounce) {
      if (debounceTimers.has(dkey)) clearTimeout(debounceTimers.get(dkey));
      debounceTimers.set(dkey, setTimeout(() => {
        debounceTimers.delete(dkey);
        doSave(name, key, value);
      }, 400));
    } else {
      doSave(name, key, value);
    }
  }

  async function loadFeatures(showStatus = true) {
    try {
      const data = await fetchJSON("/api/features");
      featuresCache = data.features || [];
      renderCollisions(data.collisions || []);
      renderFeatures();
      if (showStatus) setStatus(`${featuresCache.length} features loaded`);
    } catch (err) {
      setStatus(`Error: ${err.message}`, "err");
    }
  }

  // 2.30.2 — show shipped-wins collision warnings from /api/features
  // 2.31.0 — also surface plugin_load_errors from the three-layer merge
  function renderCollisions(collisions) {
    const bar = $("#collision-bar");
    if (!bar) return;

    let pluginErrs = [];
    fetchJSON("/api/features/collisions").then((data) => {
      pluginErrs = data.plugin_load_errors || [];
      draw();
    }).catch(() => draw());

    function draw() {
      const hasCollisions = collisions && collisions.length > 0;
      const hasErrs = pluginErrs && pluginErrs.length > 0;
      if (!hasCollisions && !hasErrs) {
        bar.hidden = true;
        bar.innerHTML = "";
        return;
      }
      bar.hidden = false;
      let html = "";
      if (hasCollisions) {
        const items = collisions.map((c) => `<li>${escapeHtml(c)}</li>`).join("");
        html += `<strong>Shadowed features:</strong> higher-precedence sources took precedence.<ul>${items}</ul>`;
      }
      if (hasErrs) {
        const rows = pluginErrs.map((e) => {
          const errs = (e.errors || []).map(escapeHtml).join("; ");
          return `<li><code>${escapeHtml(e.package)}</code> / <code>${escapeHtml(e.entry_point)}</code> — ${errs}</li>`;
        }).join("");
        html += `<strong>Plugin load errors:</strong> one or more installed concinno-skills-* packages failed to load their feature metadata.<ul>${rows}</ul>`;
      }
      bar.innerHTML = html;
    }
  }

  $("#filter").addEventListener("input", renderFeatures);
  $("#sort").addEventListener("change", renderFeatures);
  $("#only-non-default").addEventListener("change", renderFeatures);

  // ── Harness ─────────────────────────────────────────
  let harnessCache = [];

  async function loadHarness() {
    try {
      const data = await fetchJSON("/api/harness/settings");
      harnessCache = data.files || [];
      renderHarness();
    } catch (err) { setStatus(`Error: ${err.message}`, "err"); }
  }

  function renderHarness() {
    const filter = ($("#harness-filter").value || "").toLowerCase().trim();
    const bucket = $("#harness-bucket").value;
    const host = $("#harness-list"); host.innerHTML = "";
    let total = 0;
    for (const f of harnessCache) {
      const div = document.createElement("div");
      div.className = "harness-file";
      const present = f.present ? "present" : "absent";
      const perms = f.permissions || { allow: [], deny: [], ask: [] };
      const render = (name, rules) => {
        const shown = (filter ? rules.filter((r) => String(r).toLowerCase().includes(filter)) : rules);
        total += shown.length;
        return `<details${name === "allow" ? " open" : ""}>
          <summary>${name} (${shown.length}/${rules.length})</summary>
          <ul>${shown.map((r) => `<li><code>${escapeHtml(r)}</code></li>`).join("") || "<li class='muted'>(none)</li>"}</ul>
        </details>`;
      };
      div.innerHTML = `
        <h3><code>${escapeHtml(f.path)}</code> <span class="badge">${present}</span></h3>
        ${(bucket === "all" || bucket === "allow") ? render("allow", perms.allow) : ""}
        ${(bucket === "all" || bucket === "deny") ? render("deny", perms.deny) : ""}
        ${(bucket === "all" || bucket === "ask") ? render("ask", perms.ask) : ""}`;
      host.appendChild(div);
    }
    setStatus(`${total} rule(s) shown across ${harnessCache.length} file(s)`);
  }

  $("#harness-filter").addEventListener("input", renderHarness);
  $("#harness-bucket").addEventListener("change", renderHarness);

  // ── ZIQ ─────────────────────────────────────────────
  async function loadZIQ() {
    try {
      const data = await fetchJSON("/api/ziq/posterior");
      const sum = $("#ziq-summary");
      const host = $("#ziq-overrides");
      if (!data.present) {
        sum.innerHTML = `<p class="hint">ZIQ posterior file not present. Online tuning has not written any overrides yet.</p>`;
        host.innerHTML = "";
        return;
      }
      const overrides = data.overrides || [];
      sum.innerHTML = `<p><strong>${overrides.length}</strong> override(s) in <code>~/.concinno/ziq_posterior.json</code></p>`;
      if (!overrides.length) {
        host.innerHTML = `<p class="hint">No per-feature overrides yet.</p>`;
        return;
      }
      // Join against current feature params so the table shows
      // ZIQ value vs manual value side-by-side.
      const cur = new Map((featuresCache || []).map((f) => [f.name, f]));
      host.innerHTML =
        `<table class="ziq-table">
          <thead><tr><th>Feature</th><th>Key</th><th>ZIQ value</th><th>Current value</th><th>Pinned</th></tr></thead>
          <tbody>${overrides.map((o) => {
            const feat = cur.get(o.feature);
            const p = feat && feat.params ? feat.params[o.key] : null;
            const curVal = p ? (p.current === null || p.current === undefined ? p.default : p.current) : "—";
            const pinned = p && p.manual_pinned ? "🔒" : "🔄";
            return `<tr>
              <td><code>${escapeHtml(o.feature)}</code></td>
              <td><code>${escapeHtml(o.key)}</code></td>
              <td><code>${escapeHtml(JSON.stringify(o.ziq_value))}</code></td>
              <td><code>${escapeHtml(JSON.stringify(curVal))}</code></td>
              <td>${pinned}</td>
            </tr>`;
          }).join("")}</tbody>
        </table>`;
      setStatus(`${overrides.length} ZIQ override(s) loaded`);
    } catch (err) { setStatus(`Error: ${err.message}`, "err"); }
  }

  // ── State ───────────────────────────────────────────
  async function loadState() {
    try {
      const data = await fetchJSON("/api/concinno/state");
      const host = $("#state-panels"); host.innerHTML = "";
      const panels = [
        ["Release authorization", fmtPre(data.release_authorization)],
        ["Toast notifications", fmtKV({
          "Enabled": data.toast_enabled, "App ID": data.toast_app_id,
        })],
        ["Locale", fmtKV({ "Current": data.locale })],
        ["Handoff mode", fmtKV({ "Current": data.handoff_mode })],
      ];
      for (const [title, body] of panels) {
        const p = document.createElement("div");
        p.className = "state-panel";
        p.innerHTML = `<h3>${escapeHtml(title)}</h3>${body}`;
        host.appendChild(p);
      }
      setStatus("runtime state loaded");
    } catch (err) { setStatus(`Error: ${err.message}`, "err"); }
  }

  function fmtPre(v) {
    if (v === undefined || v === null) return `<p class="hint">(absent)</p>`;
    if (typeof v === "string") return `<pre>${escapeHtml(v)}</pre>`;
    return `<pre>${escapeHtml(JSON.stringify(v, null, 2))}</pre>`;
  }

  function fmtKV(kv) {
    return `<dl class="kv">${Object.entries(kv).map(([k, v]) => {
      const val = (v === null || v === undefined) ? "(unset)" : String(v);
      return `<dt>${escapeHtml(k)}</dt><dd><code>${escapeHtml(val)}</code></dd>`;
    }).join("")}</dl>`;
  }

  // ── Tab dispatch ─────────────────────────────────────
  // ── Skills ──────────────────────────────────────────
  let skillsCache = [];
  let skillsScope = null;  // null | "user" | "public" | "private" | "project"

  async function loadSkills() {
    try {
      const data = await fetchJSON("/api/skills");
      skillsCache = data.skills || [];
      renderSkillsRoots(data.roots || {});
      renderSkills();
    } catch (err) { setStatus(`Error: ${err.message}`, "err"); }
  }

  function renderSkillsRoots(roots) {
    const host = $("#skills-roots");
    if (!host) return;
    const rows = Object.entries(roots)
      .map(([scope, path]) =>
        `<div class="root-row"><span class="badge cat">${escapeHtml(scope)}</span>
         <code translate="no">${escapeHtml(path)}</code></div>`)
      .join("");
    host.innerHTML = `<details open><summary>Skill directories on this host</summary>${rows}</details>`;
  }

  function renderSkills() {
    const filter = ($("#skills-filter").value || "").toLowerCase().trim();
    const sortKey = $("#skills-sort").value;
    const host = $("#skills-list"); host.innerHTML = "";
    // Scope chip bar
    const chipBar = $("#skills-scope-chips");
    if (chipBar) {
      const scopes = Array.from(new Set(skillsCache.map((s) => s.scope || "user")));
      chipBar.innerHTML =
        `<span class="chip ${skillsScope === null ? 'chip-active' : ''}" data-scope="">All (${skillsCache.length})</span>` +
        scopes.sort().map((sc) => {
          const n = skillsCache.filter((s) => (s.scope || "user") === sc).length;
          const active = skillsScope === sc ? "chip-active" : "";
          return `<span class="chip ${active}" data-scope="${escapeHtml(sc)}">${escapeHtml(sc)} (${n})</span>`;
        }).join("");
      chipBar.querySelectorAll("[data-scope]").forEach((c) => {
        c.addEventListener("click", () => {
          const v = c.dataset.scope;
          skillsScope = v ? v : null;
          renderSkills();
        });
      });
    }
    let items = skillsCache.slice();
    if (skillsScope) items = items.filter((s) => (s.scope || "user") === skillsScope);
    if (filter) {
      items = items.filter((s) =>
        `${s.name} ${s.description || ""} ${s.example || ""}`.toLowerCase().includes(filter),
      );
    }
    const sorters = {
      name: (a, b) => a.name.localeCompare(b.name),
      enabled: (a, b) => Number(b.enabled) - Number(a.enabled) || a.name.localeCompare(b.name),
      scope: (a, b) => (a.scope || "").localeCompare(b.scope || "") || a.name.localeCompare(b.name),
    };
    items.sort(sorters[sortKey] || sorters.name);
    for (const s of items) host.appendChild(renderSkillCard(s));
    setStatus(`${items.length} / ${skillsCache.length} skills`);
  }

  function renderSkillCard(s) {
    const card = document.createElement("div");
    card.className = "feature";
    const rawScope = s.scope || "user";
    const isPlugin = rawScope.startsWith("plugin:");
    const displayScope = isPlugin ? rawScope : rawScope;
    const scopeClass = isPlugin ? "badge source-plugin" : "badge cat";
    const scopeTitle = isPlugin
      ? `From installed package — ${escapeHtml(rawScope.slice(7))}`
      : "";
    const scopeBadge = `<span class="${scopeClass}" data-skill-facet="scope" data-val="${escapeHtml(rawScope)}"${scopeTitle ? ` title="${scopeTitle}"` : ""}>${escapeHtml(displayScope)}</span>`;
    const mdBadge = s.has_skill_md
      ? '<span class="badge">SKILL.md</span>'
      : '<span class="badge" title="No SKILL.md — description is from concinno.gui.skill_descriptions fallback">bare dir</span>';
    // Prefer the longer example text if we have it, else the short description.
    const tipText = s.example && s.example !== s.description
      ? `${s.description || ""}\n\n${s.example}`
      : (s.description || "(no description)");
    card.innerHTML = `
      <header class="fhead">
        <label class="toggle">
          <input type="checkbox" data-skill="${s.name}" ${s.enabled ? "checked" : ""}>
          <code translate="no">${escapeHtml(s.name)}</code>
        </label>
        ${scopeBadge}
        ${mdBadge}
        <button type="button" class="help-btn" aria-label="help">?</button>
      </header>
      <p class="desc">${escapeHtml(s.description || "(no description)")}</p>
      <div class="help-tooltip">${escapeHtml(tipText)}</div>`;
    card.querySelector("input[data-skill]").addEventListener("change", async (e) => {
      const enabled = e.target.checked;
      setStatus(`${s.name}: toggling…`);
      try {
        await fetchJSON(`/api/skills/${encodeURIComponent(s.name)}`, {
          method: "POST",
          body: JSON.stringify({ enabled }),
        });
        s.enabled = enabled;
        setStatus(`${s.name} ${enabled ? "enabled" : "disabled"} @ ${new Date().toLocaleTimeString()}`, "ok");
      } catch (err) {
        setStatus(`Error: ${err.message}`, "err");
      }
    });
    // Scope badge click → filter
    const scopeEl = card.querySelector('[data-skill-facet="scope"]');
    if (scopeEl) scopeEl.addEventListener("click", () => {
      skillsScope = skillsScope === scopeEl.dataset.val ? null : scopeEl.dataset.val;
      renderSkills();
    });
    // ? tooltip JS-wired (same pattern as feature cards)
    const helpBtn = card.querySelector(".help-btn");
    const helpTip = card.querySelector(".help-tooltip");
    if (helpBtn && helpTip) {
      const show = () => helpTip.classList.add("show");
      const hide = () => helpTip.classList.remove("show");
      helpBtn.addEventListener("mouseenter", show);
      helpBtn.addEventListener("mouseleave", hide);
      helpBtn.addEventListener("focus", show);
      helpBtn.addEventListener("blur", hide);
      helpBtn.addEventListener("click", (e) => {
        e.preventDefault();
        helpTip.classList.toggle("show");
      });
      helpTip.addEventListener("mouseenter", show);
      helpTip.addEventListener("mouseleave", hide);
    }
    return card;
  }

  $("#skills-filter").addEventListener("input", renderSkills);
  $("#skills-sort").addEventListener("change", renderSkills);

  // ── CC Commands ─────────────────────────────────────
  let commandsCache = [];

  async function loadCommands() {
    try {
      const data = await fetchJSON("/api/commands");
      commandsCache = data.commands || [];
      renderCommands();
    } catch (err) { setStatus(`Error: ${err.message}`, "err"); }
  }

  function renderCommands() {
    const filter = ($("#commands-filter").value || "").toLowerCase().trim();
    const host = $("#commands-list"); host.innerHTML = "";
    // Populate the autocomplete datalist — browser-native combobox
    // dropdown. Every slug + description becomes a suggestion; typing
    // ``/concinno`` then spacebar exposes the full Concinno set without
    // having to open the CC ``/`` menu.
    const dl = $("#commands-datalist");
    if (dl) {
      dl.innerHTML = commandsCache.map((c) => {
        const tag = c.managed ? " [concinno]" : "";
        return `<option value="/${escapeHtml(c.slug)}">${escapeHtml(c.description || "")}${tag}</option>`;
      }).join("");
    }
    const items = commandsCache.filter((c) =>
      !filter
      || `${c.slug} ${c.description}`.toLowerCase().includes(filter.replace(/^\//, ""))
      || `/${c.slug}`.toLowerCase().includes(filter),
    );
    for (const c of items) {
      const row = document.createElement("div");
      row.className = "harness-file";
      const tag = c.managed
        ? '<span class="badge ziq">concinno</span>'
        : '<span class="badge">user</span>';
      row.innerHTML = `
        <h3><code translate="no">/${escapeHtml(c.slug)}</code> ${tag}</h3>
        <p class="desc">${escapeHtml(c.description || "(no description)")}</p>
        <p class="desc" translate="no"><small>${escapeHtml(c.path)}</small></p>`;
      host.appendChild(row);
    }
    setStatus(`${items.length} / ${commandsCache.length} commands`);
  }

  document.addEventListener("click", async (e) => {
    if (e.target && e.target.id === "commands-sync") {
      setStatus("resyncing concinno commands…");
      try {
        const rep = await fetchJSON("/api/commands/sync", { method: "POST" });
        setStatus(`synced: ${rep.written.length} written, ${rep.unchanged.length} unchanged, ${rep.removed_orphans.length} removed`, "ok");
        loadCommands();
      } catch (err) { setStatus(`Error: ${err.message}`, "err"); }
    }
  });

  const cmdFilter = $("#commands-filter");
  if (cmdFilter) cmdFilter.addEventListener("input", renderCommands);

  // ── Marketplace (4.6.0) ─────────────────────────────────
  let marketplaceCache = { installed: [], available: [], cache_age_sec: 0,
    pypi_reachable: true, release_auth_disabled: false };

  async function loadMarketplace() {
    try {
      marketplaceCache = await fetchJSON("/api/skills/marketplace");
      renderMarketplaceMeta();
      renderMarketplaceList();
    } catch (err) { setStatus(`Error: ${err.message}`, "err"); }
  }

  function renderMarketplaceMeta() {
    const host = $("#marketplace-meta");
    if (!host) return;
    const reach = marketplaceCache.pypi_reachable
      ? '<span class="badge effect-immediate">PyPI: reachable</span>'
      : '<span class="badge effect-process">PyPI: unreachable (cache only)</span>';
    const age = marketplaceCache.cache_age_sec || 0;
    const auth = marketplaceCache.release_auth_disabled
      ? '<span class="badge ziq">release_auth: disabled (no twice-click)</span>'
      : '<span class="badge effect-session">release_auth: enforced</span>';
    host.innerHTML = `<div class="root-row">${reach} ${auth}
      <code translate="no">cache age ${Math.floor(age/60)}m ${age%60}s</code></div>`;
  }

  function renderMarketplaceList() {
    const filter = ($("#marketplace-filter").value || "").toLowerCase().trim();
    const kindFilter = $("#marketplace-kind").value;
    function filterRows(rows) {
      return rows.filter((r) => {
        if (kindFilter !== "all" && r.kind !== kindFilter) return false;
        if (!filter) return true;
        return `${r.name} ${r.summary}`.toLowerCase().includes(filter);
      });
    }
    const installed = filterRows(marketplaceCache.installed || []);
    const available = filterRows(marketplaceCache.available || []);
    const installedHost = $("#marketplace-installed");
    const availableHost = $("#marketplace-available");
    installedHost.innerHTML = "";
    availableHost.innerHTML = "";
    for (const r of installed) installedHost.appendChild(renderMarketplaceCard(r, true));
    for (const r of available) availableHost.appendChild(renderMarketplaceCard(r, false));
    setStatus(`marketplace: ${installed.length} installed / ${available.length} available`);
  }

  function renderMarketplaceCard(row, installed) {
    const card = document.createElement("div");
    card.className = "feature";
    const kindBadge = row.kind === "skill-pkg"
      ? '<span class="badge ziq">skill-pkg</span>'
      : (row.kind === "hook-pkg"
        ? '<span class="badge cat">hook-pkg</span>'
        : '<span class="badge">unknown</span>');
    const stateBadge = row.install_state === "outdated"
      ? '<span class="badge effect-session">outdated</span>'
      : (row.install_state === "broken"
        ? '<span class="badge effect-process">broken</span>'
        : "");
    const wired = (row.wired_consumers || []).map((w) =>
      `<code translate="no">${escapeHtml(w)}</code>`).join(", ") || "(none)";
    const versions = installed
      ? `${escapeHtml(row.version_installed || "?")} → ${escapeHtml(row.version_latest || "?")}`
      : escapeHtml(row.version_latest || "(unknown)");
    const action = installed
      ? `<button type="button" class="lang-switch" data-mp-uninstall="${escapeHtml(row.name)}">Uninstall</button>`
      : `<button type="button" class="lang-switch" data-mp-install="${escapeHtml(row.name)}" data-mp-version="${escapeHtml(row.version_latest || "")}">Install</button>`;
    card.innerHTML = `
      <header class="fhead">
        <code translate="no">${escapeHtml(row.name)}</code>
        ${kindBadge}${stateBadge}
        <span class="badge">${versions}</span>
        ${action}
      </header>
      <p class="desc">${escapeHtml(row.summary || "(no summary)")}</p>
      <p class="desc"><small>wired: ${wired}</small></p>`;
    return card;
  }

  async function confirmAndAct(name, version, op) {
    if (!marketplaceCache.release_auth_disabled) {
      const cmd = op === "install"
        ? `pip install ${name}${version ? `==${version}` : ""}`
        : `pip uninstall -y ${name}`;
      const ok1 = window.confirm(`About to run: ${cmd}\n\nClick OK to confirm.`);
      if (!ok1) return;
      const ok2 = window.confirm(`Confirm again: ${cmd}\n\nThis will run a privileged subprocess. Proceed?`);
      if (!ok2) return;
    }
    setStatus(`${op} ${name}…`);
    try {
      const body = { package: name, confirm_token: "ui-confirmed" };
      if (op === "install" && version) body.version = version;
      const url = `/api/skills/marketplace/${op}`;
      const r = await fetchJSON(url, { method: "POST", body: JSON.stringify(body) });
      if (r.ok) {
        setStatus(`${op} ${name} ok`, "ok");
        loadMarketplace();
      } else {
        setStatus(`${op} ${name} FAILED: ${(r.stderr || "").slice(0, 200)}`, "err");
      }
    } catch (err) { setStatus(`Error: ${err.message}`, "err"); }
  }

  document.addEventListener("click", async (e) => {
    const tgt = e.target;
    if (!(tgt instanceof HTMLElement)) return;
    if (tgt.dataset.mpInstall) {
      await confirmAndAct(tgt.dataset.mpInstall, tgt.dataset.mpVersion || null, "install");
    } else if (tgt.dataset.mpUninstall) {
      await confirmAndAct(tgt.dataset.mpUninstall, null, "uninstall");
    } else if (tgt.id === "marketplace-refresh") {
      try {
        await fetchJSON("/api/skills/marketplace/refresh");
        loadMarketplace();
      } catch (err) { setStatus(`Error: ${err.message}`, "err"); }
    }
  });

  const mpFilter = $("#marketplace-filter");
  if (mpFilter) mpFilter.addEventListener("input", renderMarketplaceList);
  const mpKind = $("#marketplace-kind");
  if (mpKind) mpKind.addEventListener("change", renderMarketplaceList);

  function loadTab(tab) {
    switch (tab) {
      case "features": return loadFeatures();
      case "skills": return loadSkills();
      case "marketplace": return loadMarketplace();
      case "commands": return loadCommands();
      case "harness": return loadHarness();
      case "ziq": return Promise.all([loadFeatures(false), loadZIQ()]);
      case "state": return loadState();
    }
  }

  // ── Live polling: re-fetch active tab when digest changes ──
  async function tick() {
    try {
      const d = await fetchJSON("/api/features/digest");
      if (lastDigest !== null && d.digest !== lastDigest) {
        // Something mutated: refresh active tab.
        const active = $$("nav.tabs button.active")[0];
        if (active) loadTab(active.dataset.tab);
      }
      lastDigest = d.digest;
      $("#live-indicator").classList.add("on");
    } catch {
      $("#live-indicator").classList.remove("on");
    }
  }
  setInterval(tick, 3000);
  tick();

  loadFeatures();

  // 2.30.2 — ?tab= + ?highlight= URL query-param support.
  // Called by `concinno skills new` and `concinno features register`
  // so the post-scaffold URL lands on the right tab and pulses the
  // new card. Runs once at boot; later tab switches ignore the query.
  (function applyUrlParams() {
    const params = new URLSearchParams(window.location.search);
    const tab = params.get("tab");
    const highlight = params.get("highlight");
    if (tab) {
      const btn = $$(`nav.tabs button[data-tab="${tab}"]`)[0];
      if (btn) btn.click();
    }
    if (!highlight) return;
    // Poll briefly for the card to land in the DOM (loadFeatures /
    // loadSkills are async). Give up after ~3s — the digest-poll will
    // eventually render it and the user can refresh manually.
    const target = String(highlight);
    let tries = 0;
    const scroller = setInterval(() => {
      tries += 1;
      const card = document.querySelector(`[data-name="${CSS.escape(target)}"]`);
      if (card) {
        clearInterval(scroller);
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.add("pulse-highlight");
        setTimeout(() => card.classList.remove("pulse-highlight"), 2600);
      } else if (tries > 20) {
        clearInterval(scroller);
      }
    }, 150);
  })();
})();
