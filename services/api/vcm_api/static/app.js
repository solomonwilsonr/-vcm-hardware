/* VCM dashboard. Vanilla ES2020, no build step, no network beyond this origin's API. */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ dom */

  const SVG_NS = "http://www.w3.org/2000/svg";

  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    apply(e, attrs);
    add(e, kids);
    return e;
  }

  function s(tag, attrs, ...kids) {
    const e = document.createElementNS(SVG_NS, tag);
    apply(e, attrs);
    add(e, kids);
    return e;
  }

  function apply(e, attrs) {
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") e.setAttribute("class", v);
      else if (k === "text") e.textContent = String(v);
      else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
      else if (e.namespaceURI === SVG_NS) e.setAttributeNS(null, k, String(v));
      else e.setAttribute(k, v === true ? "" : String(v));
    }
  }

  function add(e, kids) {
    for (const k of kids.flat(4)) {
      if (k === null || k === undefined || k === false) continue;
      e.append(k instanceof Node ? k : document.createTextNode(String(k)));
    }
  }

  const clear = (e) => { while (e.firstChild) e.removeChild(e.firstChild); return e; };
  /* Null-safe container fill: unlike Node.append, nulls are dropped, not stringified. */
  const mount = (e, ...kids) => { add(e, kids); return e; };
  const byId = (id) => document.getElementById(id);

  /* ------------------------------------------------------------------ api */

  /* The API token lives only in this browser. Storage can be unavailable (private windows,
     blocked site data), so every access is guarded and a missing token just means a prompt. */
  const TOKEN_KEY = "vcm-api-token";
  function storedToken() {
    try { return window.localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }
  function storeToken(t) {
    try { if (t) window.localStorage.setItem(TOKEN_KEY, t); else window.localStorage.removeItem(TOKEN_KEY); }
    catch (e) { /* not persisted; asked again next load */ }
  }
  let sessionToken = storedToken();
  /* Several views load in parallel; every 401 waits on the same prompt rather than each asking. */
  let tokenAsk = null;
  function askToken() {
    if (!tokenAsk) {
      tokenAsk = Promise.resolve().then(() => {
        const t = window.prompt("This VCM server requires an API token (create one with `vcm token create`):");
        if (t && t.trim()) { sessionToken = t.trim(); storeToken(sessionToken); return true; }
        return false;
      }).finally(() => { setTimeout(() => { tokenAsk = null; }, 0); });
    }
    return tokenAsk;
  }

  async function api(path, opts, retried) {
    const init = Object.assign({ headers: { accept: "application/json" } }, opts);
    if (init.json !== undefined) {
      init.method = init.method || "POST";
      init.headers = Object.assign({}, init.headers, { "content-type": "application/json" });
      init.body = JSON.stringify(init.json);
      delete init.json;
    }
    const sentWith = sessionToken;
    if (sentWith) {
      init.headers = Object.assign({}, init.headers, { authorization: "Bearer " + sentWith });
    }
    const r = await fetch(path, init);
    if (r.status === 401 && !retried) {
      if (tokenAsk) await tokenAsk;
      if (sessionToken && sessionToken !== sentWith) return api(path, opts, true);
      if (await askToken()) return api(path, opts, true);
    }
    const ct = r.headers.get("content-type") || "";
    const payload = ct.includes("json") ? await r.json().catch(() => null) : await r.text();
    if (!r.ok) {
      const detail = payload && payload.detail ? payload.detail : r.statusText;
      throw new Error(detail + " (" + r.status + ")");
    }
    return payload;
  }

  /* --------------------------------------------------------------- format */

  const shortHash = (x, n) => (x ? String(x).slice(0, n || 12) : "");
  const plural = (n, one, many) => n + " " + (n === 1 ? one : many || one + "s");

  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const p = (v) => String(v).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  const bytes = (n) => (n >= 1024 ? (n / 1024).toFixed(1) + " kB" : n + " B");

  /* The domain is whatever the repository holds, so nothing here assumes a PCB:
     positions are printed from whatever keys the payload carries, and example
     component references come from the data, never from a hard-coded list. */
  function position(p) {
    if (!p || typeof p !== "object") return "\u2014";
    if (p.x !== undefined && p.y !== undefined) return p.x + ", " + p.y;
    const vals = Object.entries(p)
      .filter(([k, v]) => k !== "layer" && (typeof v === "number" || typeof v === "string"))
      .map(([k, v]) => k + " " + v);
    return vals.length ? vals.join(", ") : "\u2014";
  }

  function exampleRef() {
    for (const r of S.revs) {
      if (r.changed_components && r.changed_components.length) return r.changed_components[0];
    }
    return null;
  }

  /* ------------------------------------------- confidence class + severity */

  const CONF = {
    verified: {
      label: "Verified",
      blurb: "Derived from parsed design data, declared project links or stored evidence.",
      never: "Not a judgement. If the files say it, it is here.",
    },
    rule_based: {
      label: "Rule-based",
      blurb: "Heuristic from the engineering rules and the component database.",
      never: "An inference, not a measurement. Check the cited part data yourself.",
    },
    speculative: {
      label: "Speculative",
      blurb: "A prediction. Not established by any data in this repository.",
      never: "Treat as a prompt to go and test, never as a finding.",
    },
  };
  const CONF_ORDER = ["verified", "rule_based", "speculative"];

  /* Shape is the primary non-colour cue: filled square / filled triangle /
     dotted open diamond. Border style (solid / dashed / dotted) repeats it,
     and every chip also carries the class name in text. */
  function cmark(cls, size) {
    const n = size || 12;
    const g = { class: "cmark", width: n, height: n, viewBox: "0 0 12 12",
      "aria-hidden": "true", focusable: "false" };
    if (cls === "verified") {
      return s("svg", g, s("rect", { x: 2, y: 2, width: 8, height: 8, fill: "currentColor" }));
    }
    if (cls === "rule_based") {
      return s("svg", g, s("path", { d: "M6 1.6 L10.8 10.4 L1.2 10.4 Z", fill: "currentColor" }));
    }
    return s("svg", g, s("path", {
      d: "M6 1.3 L10.7 6 L6 10.7 L1.3 6 Z",
      fill: "none", stroke: "currentColor", "stroke-width": 1.5, "stroke-dasharray": "1.7 1.5",
    }));
  }

  function cchip(cls) {
    const meta = CONF[cls] || { label: cls };
    return h("span", { class: "cchip " + cls, title: meta.blurb }, cmark(cls), meta.label);
  }

  const SEV = { critical: "!!", warning: "!", info: "i" };
  const SEV_RANK = { critical: 0, warning: 1, info: 2 };

  function schip(sev) {
    return h("span", { class: "schip " + sev },
      h("span", { class: "g", "aria-hidden": "true" }, SEV[sev] || "?"), sev);
  }

  function refChips(refs, onPick) {
    if (!refs || !refs.length) return null;
    return h("div", { class: "refs" }, refs.map((r) =>
      onPick
        ? h("button", { class: "ref", type: "button", title: "Trace " + r + " to built units",
            onclick: () => onPick(r) }, r)
        : h("span", { class: "ref" }, r)));
  }

  /* ------------------------------------------------------------- evidence */

  function evidenceRow(e) {
    if (e.type === "file") {
      return h("tr", {},
        h("td", { class: "t" }, "file"),
        h("td", {}, e.path, " ",
          h("span", { class: "h", title: "sha256 " + e.hash }, "@" + shortHash(e.hash, 16)),
          e.revision ? h("span", { class: "dim" }, "  rev " + shortHash(e.revision, 10)) : null));
    }
    if (e.type === "commit") {
      return h("tr", {},
        h("td", { class: "t" }, "commit"),
        h("td", {}, h("span", { class: "h", title: e.revision }, shortHash(e.revision, 16)),
          e.role ? h("span", { class: "dim" }, "  " + e.role) : null));
    }
    if (e.type === "ref") {
      return h("tr", {}, h("td", { class: "t" }, "ref"), h("td", {}, e.ref));
    }
    if (e.type === "db") {
      return h("tr", {},
        h("td", { class: "t" }, "part db"),
        h("td", {}, e.part, h("span", { class: "dim" }, "  bundled component database")));
    }
    return h("tr", {}, h("td", { class: "t" }, e.type),
      h("td", {}, JSON.stringify(e)));
  }

  function evidenceBlock(list, label) {
    if (!list || !list.length) {
      return h("p", { class: "no-ev" }, "no evidence links recorded");
    }
    return h("details", { class: "evidence" },
      h("summary", {}, (label || "Evidence") + " · " + list.length),
      h("table", { class: "ev" }, h("tbody", {}, list.map(evidenceRow))));
  }

  /* ----------------------------------------------------------- primitives */

  function panel(title, opts, ...body) {
    const o = opts || {};
    return h("section", { class: "panel" },
      title === null ? null : h("header", {},
        h("h2", {}, title),
        o.count !== undefined && o.count !== null ? h("span", { class: "count" }, o.count) : null,
        o.extra || null),
      h("div", { class: "body" + (o.flush ? " flush" : "") }, body));
  }

  // Plain-language hover text for the numbers, so a first-time reader is never left guessing.
  const STAT_HINTS = {
    "components changed": "Parts that were replaced or had a value changed between the two revisions.",
    "form factors": "Parts whose slot or form factor changed. This usually means physical rework.",
    "repositioned": "Parts that moved to a different position.",
    "line items": "Quantity changes: the same part, more or fewer of it.",
    "connections": "Power domains, PCIe roots or fabrics that were added, removed or changed.",
    "files": "Pinned files (firmware, docs, inventory) that were added, removed or modified.",
    "findings": "Everything the impact analysis reported, across all severities.",
    "critical": "Problems to fix before this change is rolled out.",
    "catalog values checked": "Catalog values a named person has verified against a datasheet.",
    "catalog": "Share of the changed parts that the component catalog knows about.",
    "validation": "Built-in checks, before and after the change."
  };

  function stat(n, label, tone) {
    const hint = STAT_HINTS[label];
    return h("div", Object.assign({ class: "stat" + (n === 0 || n === "0" ? " zero" : "") },
        hint ? { title: hint } : {}),
      h("div", { class: "n" + (tone ? " " + tone : "") }, n),
      h("div", { class: "l" }, label));
  }

  function field(label, control) {
    return h("label", { class: "field" }, h("span", {}, label), control);
  }

  function select(id, options, value, onchange) {
    const sel = h("select", { id: id, onchange: onchange },
      options.map((o) => h("option", { value: o.value }, o.label)));
    if (value !== undefined && value !== null) sel.value = value;
    return sel;
  }

  const errBox = (e) => h("p", { class: "err" }, "Error: " + e.message);
  const loading = (what) => h("p", { class: "loading" }, what || "Loading");
  const empty = (txt) => h("p", { class: "empty" }, txt);

  /* Line-art for empty states: a picture of the thing that is missing, then what to do about it.
     Decorative, so hidden from assistive technology; the text carries the meaning. */
  const ART = {
    release: ["M20 24l40-18 40 18v34l-40 18-40-18z", "M20 24l40 18 40-18M60 42v34", "M40 15l40 18"],
    review: ["M30 14v52", "M30 30h38a14 14 0 0114 14v6", "M30 66h38", "M30 14m-6 0a6 6 0 1012 0 6 6 0 10-12 0",
      "M30 66m-6 0a6 6 0 1012 0 6 6 0 10-12 0", "M82 50m-6 0a6 6 0 1012 0 6 6 0 10-12 0"],
    agent: ["M26 8h46l18 18v46H26z", "M72 8v18h18", "M38 38h38M38 50h38M38 62h22", "M84 66m-12 0a12 12 0 1024 0 12 12 0 10-24 0",
      "M78 66l5 5 9-10"],
    rack: ["M28 6h64v20H28zM28 30h64v20H28zM28 54h64v20H28z", "M36 16h.01M36 40h.01M36 64h.01",
      "M48 16h28M48 40h28M48 64h28", "M22 78h76"],
  };
  function emptyArt(kind, txt) {
    const svg = s("svg", { class: "emptyart-svg", viewBox: "0 0 120 80", "aria-hidden": "true" },
      (ART[kind] || []).map((d) => s("path", { d: d, pathLength: 1 })));
    return h("div", { class: "emptyart" }, svg, h("p", { class: "empty" }, txt));
  }

  function table(headers, rows) {
    return h("div", { class: "scroll" },
      h("table", { class: "data" },
        h("thead", {}, h("tr", {}, headers.map((x) =>
          h("th", {}, x === "" ? h("span", { class: "sr" }, "Actions") : x)))),
        h("tbody", {}, rows)));
  }

  function statusChip(status) {
    const k = status === "pass" || status === true ? "pass"
      : status === "fail" || status === false ? "fail" : "neutral";
    return h("span", { class: "statuschip " + k },
      String(status === true ? "ok" : status === false ? "fail" : status));
  }

  /* ---------------------------------------------------------------- state */

  const S = {
    repo: null,
    revs: [],
    devices: [],
    cmp: { a: null, b: null },
    lineage: { ref: null, rev: "HEAD", depth: 2, serial: null },
    releases: { selected: null },
    review: { selected: null },
    fleet: { upgrade: null },
    ui: {},
    view: null,
  };

  const revLabel = (r) =>
    r.short + (r.tags.length ? "  [" + r.tags.join(", ") + "]" : "") + "  " + r.message;

  const revOptions = () => S.revs.map((r) => ({ value: r.id, label: revLabel(r) }));

  const revShort = (id) => {
    const r = S.revs.find((x) => x.id === id);
    return r ? (r.tags[0] || r.short) : shortHash(id, 10);
  };

  /* -------------------------------------------------------------- compare */

  function compareView(main) {
    const opts = revOptions();
    const bar = h("div", { class: "toolbar" },
      field("Base", select("cmp-a", opts, S.cmp.a, (e) => { S.cmp.a = e.target.value; })),
      h("button", { class: "swap", type: "button", title: "Swap base and head",
        onclick: () => {
          const a = byId("cmp-a"), b = byId("cmp-b");
          const t = a.value; a.value = b.value; b.value = t;
          S.cmp.a = a.value; S.cmp.b = b.value;
          runCompare();
        } }, "⇄"),
      field("Head", select("cmp-b", opts, S.cmp.b, (e) => { S.cmp.b = e.target.value; })),
      h("button", { class: "primary", type: "button", id: "cmp-go", onclick: runCompare },
        "Compare"));

    const out = h("div", { id: "cmp-out" }, loading("Comparing"));

    mount(main,
      h("div", { class: "view-head" },
        h("h1", {}, "Compare revisions"),
        h("p", {}, "What changed between two revisions, what else it touches, and on what " +
          "evidence. Every impact finding carries exactly one confidence class.")),
      panel("Revision pair", { count: plural(S.revs.length, "revision") }, bar),
      out);
    S.ui.cmpOut = out;
    runCompare();
  }

  async function runCompare() {
    const out = S.ui.cmpOut;
    if (!out) return;
    const a = S.cmp.a, b = S.cmp.b;
    clear(out).append(loading("Diffing " + revShort(a) + " → " + revShort(b)));
    const go = byId("cmp-go");
    if (go) go.disabled = true;
    const q = "a=" + encodeURIComponent(a) + "&b=" + encodeURIComponent(b);
    try {
      const [diff, impact, vcmp] = await Promise.all([
        api("/api/diff?" + q),
        api("/api/impact?" + q),
        api("/api/validation/compare?" + q).catch(() => null),
      ]);
      mount(clear(out),
        verdictBlock(impact, a, b),
        statStrip(diff, impact, vcmp),
        h("div", { class: "grid-2" }, diffColumn(diff), impactColumn(impact)),
        retestPanel(impact));
    } catch (e) {
      clear(out).append(errBox(e));
    } finally {
      if (go) go.disabled = false;
    }
  }

  function verdictBlock(impact, a, b) {
    const f = impact.findings.slice().sort((x, y) => SEV_RANK[x.severity] - SEV_RANK[y.severity]);
    const top = f[0];
    const by = impact.summary.by_class || {};
    const counts = h("div", { class: "vmeta" },
      CONF_ORDER.map((c) => h("span", { class: "cchip " + c }, cmark(c),
        (by[c] || 0) + " " + CONF[c].label)));
    if (!top) {
      return h("div", { class: "verdict clean" },
        h("div", {},
          h("div", { class: "vtitle" }, "No impact findings"),
          h("div", { class: "vdetail" }, "Nothing in the design data links this change to " +
            "anything else."), counts));
    }
    return h("div", { class: "verdict " + top.severity },
      h("div", { style: "min-width:0" },
        h("div", { class: "vmeta" }, schip(top.severity), cchip(top.confidence_class),
          h("span", { class: "fid mono" }, top.id),
          h("span", { class: "fid mono" }, revShort(a) + " → " + revShort(b))),
        h("div", { class: "vtitle" }, top.title),
        h("div", { class: "vdetail" }, top.detail),
        counts));
  }

  function statStrip(diff, impact, vcmp) {
    const d = diff.summary;
    const sev = impact.summary.by_severity || {};
    const nets = (d.nets_added || 0) + (d.nets_removed || 0) + (d.nets_changed || 0);
    const arts = diff.artifacts;
    const artn = arts.added.length + arts.removed.length + arts.modified.length;
    return h("div", { class: "stats" },
      stat(d.replaced + d.value_changed, "components changed"),
      stat(d.footprint_changed, "form factors"),
      stat(d.moved, "repositioned"),
      stat(d.count_changes, "line items"),
      stat(nets, "connections"),
      stat(artn, "files"),
      stat(impact.findings.length, "findings"),
      stat(sev.critical || 0, "critical", sev.critical ? "bad" : null),
      impact.summary.trust && impact.summary.trust.catalog_values
        ? stat(impact.summary.trust.catalog_verified + "/" + impact.summary.trust.catalog_values,
            "catalog values checked", impact.summary.trust.catalog_verified ? null : "bad")
        : null,
      stat(impact.catalog && typeof impact.catalog.ratio === "number"
        ? Math.round(impact.catalog.ratio * 100) + "%" : "n/a", "catalog"),
      stat(vcmp ? vcmp.status.from + "→" + vcmp.status.to : "n/a", "validation",
        "txt " + (vcmp && vcmp.regression ? "bad" : vcmp ? "ok" : "")));
  }

  /* --------------------------------------------------------- diff column */

  function diffColumn(d) {
    const col = h("div", {});
    const EMPTY = [];
    const b = Object.assign({ replaced: EMPTY, value_changed: EMPTY, added: EMPTY,
      removed: EMPTY, count_changes: EMPTY }, d.bom);
    const p = Object.assign({ footprint_changes: EMPTY, moved: EMPTY }, d.pcb);
    const n = Object.assign({ added: EMPTY, removed: EMPTY, changed: EMPTY }, d.nets);
    const skipped = [];

    if (b.replaced.length) {
      col.append(panel("Components replaced", { count: b.replaced.length, flush: true },
        table(["Ref", "From", "To", "Changed fields"], b.replaced.map((x) =>
          h("tr", {},
            h("td", { class: "m" }, x.ref),
            h("td", {}, h("span", { class: "old" }, x.old.value),
              h("div", { class: "mono", style: "color:var(--fg-faint)" }, x.old.mpn || "—")),
            h("td", {}, h("span", { class: "new" }, x.new.value),
              h("div", { class: "mono", style: "color:var(--fg-faint)" }, x.new.mpn || "—")),
            h("td", {}, refChips(x.changed_fields)))))));
    } else { skipped.push("replacements"); }

    if (b.value_changed.length) {
      col.append(panel("Values changed", { count: b.value_changed.length, flush: true },
        table(["Ref", "From", "To"], b.value_changed.map((x) =>
          h("tr", {}, h("td", { class: "m" }, x.ref),
            h("td", { class: "m old" }, x.old_value),
            h("td", { class: "m new" }, x.new_value))))));
    } else { skipped.push("value edits"); }

    if (b.added.length || b.removed.length) {
      col.append(panel("Components added / removed",
        { count: b.added.length + b.removed.length, flush: true },
        table(["", "Ref", "Value"], [
          b.added.map((x) => h("tr", {}, h("td", { class: "m new" }, "+"),
            h("td", { class: "m" }, x.ref), h("td", {}, x.value))),
          b.removed.map((x) => h("tr", {}, h("td", { class: "m old" }, "−"),
            h("td", { class: "m" }, x.ref), h("td", {}, x.value))),
        ])));
    } else { skipped.push("additions/removals"); }

    if (p.footprint_changes.length) {
      col.append(panel("Form factor / slot changes", { count: p.footprint_changes.length, flush: true },
        table(["Ref", "From → To"], p.footprint_changes.map((x) =>
          h("tr", {}, h("td", { class: "m" }, x.ref),
            h("td", { class: "m" }, h("div", { class: "old" }, x.old),
              h("div", { class: "new" }, "→ " + x.new)))))));
    }

    if (p.moved.length) {
      col.append(panel("Position changes", { count: p.moved.length, flush: true },
        table(["Ref", "Delta", "From", "To", "Where"], p.moved.map((x) =>
          h("tr", {}, h("td", { class: "m" }, x.ref),
            h("td", { class: "num" }, typeof x.distance_mm === "number"
              ? x.distance_mm.toFixed(2) : "—"),
            h("td", { class: "m" }, position(x.old)),
            h("td", { class: "m" }, position(x.new)),
            h("td", { class: "m" }, (x.new && x.new.layer) || "—"))))));
    }

    const netRows = [
      n.added.map((x) => ["+", x.net || x, ""]),
      n.removed.map((x) => ["−", x.net || x, ""]),
      n.changed.map((x) => ["~", x.net || x,
        (x.added_refs || []).concat(x.removed_refs || []).join(" ")]),
    ].flat();
    if (netRows.length) {
      col.append(panel("Connections", { count: netRows.length, flush: true },
        table(["", "Connection", "Components"], netRows.map((r) =>
          h("tr", {}, h("td", { class: "m" }, r[0]), h("td", { class: "m" }, r[1]),
            h("td", { class: "m" }, r[2]))))));
    } else { skipped.push("connection changes"); }

    if (b.count_changes.length) {
      col.append(panel("Line-item quantities", { count: b.count_changes.length, flush: true },
        table(["Value", "Form factor", "Was", "Now"], b.count_changes.map((x) =>
          h("tr", {}, h("td", { class: "m" }, x.value),
            h("td", { class: "m", style: "color:var(--fg-faint)" }, x.footprint),
            h("td", { class: "num old" }, x.old), h("td", { class: "num new" }, x.new))))));
    }

    const a = d.artifacts;
    col.append(panel("Files", { count: a.added.length + a.removed.length + a.modified.length,
      flush: true },
      table(["", "Path", "Category", "sha256"], [
        a.added.map((x) => h("tr", {}, h("td", { class: "m new" }, "+"),
          h("td", { class: "m" }, x.path), h("td", { class: "m" }, x.category),
          h("td", { class: "m", style: "color:var(--fg-faint)" }, "new file"))),
        a.removed.map((x) => h("tr", {}, h("td", { class: "m old" }, "−"),
          h("td", { class: "m" }, x.path), h("td", { class: "m" }, x.category),
          h("td", { class: "m", style: "color:var(--fg-faint)" }, "removed"))),
        a.modified.map((x) => h("tr", {}, h("td", { class: "m" }, "~"),
          h("td", { class: "m" }, x.path), h("td", { class: "m" }, x.category),
          h("td", { class: "m" },
            h("div", { class: "old", title: x.old_hash }, shortHash(x.old_hash, 16)),
            h("div", { class: "new", title: x.new_hash }, "→ " + shortHash(x.new_hash, 16))))),
      ])));

    if (skipped.length) {
      col.append(h("p", { class: "empty" }, "No " + skipped.join(", ") + " in this change."));
    }
    return col;
  }

  /* ------------------------------------------------------- impact column */

  function impactColumn(rep) {
    const by = rep.summary.by_class || {};
    const legend = panel("How to read a finding", {
      extra: h("span", { class: "count" },
        "LLM provider: " + (rep.provider || "none") +
        (rep.provider === "none" ? " · fully offline" : "")),
    }, h("div", { class: "legend" }, CONF_ORDER.map((c) =>
      h("div", { class: "lcard " + c },
        h("span", { class: "cchip " + c }, cmark(c), CONF[c].label + " · " + (by[c] || 0)),
        h("p", {}, CONF[c].blurb),
        h("span", { class: "never" }, CONF[c].never)))));

    const bands = CONF_ORDER.map((c) => {
      const items = rep.findings
        .filter((f) => f.confidence_class === c)
        .sort((x, y) => SEV_RANK[x.severity] - SEV_RANK[y.severity]);
      return h("section", { class: "band " + c },
        h("header", {},
          h("span", { class: "cchip " + c }, cmark(c, 13), CONF[c].label),
          h("span", { class: "count" }, plural(items.length, "finding")),
          h("span", { class: "blurb" }, CONF[c].blurb)),
        h("div", { class: "findings" }, items.length
          ? items.map(findingRow)
          : h("p", { class: "empty", style: "padding:10px 12px" }, "No findings in this class.")));
    });

    return h("div", {},
      panel("Impact", {
        count: plural(rep.findings.length, "finding"),
        extra: h("span", { class: "count" },
          CONF_ORDER.map((c) => (by[c] || 0)).join(" / ") + " by class"),
      }, h("div", {}, bands)),
      catalogPanel(rep.catalog),
      legend);
  }

  /* Catalog coverage: how much of this change the component catalog actually knows.
     Rendered from whatever buckets the payload carries, so it survives a change of
     domain or of bucket names. */
  const BUCKET = {
    known: "In the catalog",
    unknown: "Not in the catalog",
    declared: "Declared in the project",
    passive: "No catalog entry needed",
    unreviewed: "Catalog values not human-reviewed",
    reviewed_refs: "Catalog values human-reviewed",
  };

  function catalogPanel(cat) {
    if (!cat) return null;
    const pct = typeof cat.ratio === "number" ? Math.round(cat.ratio * 100) : null;
    const buckets = Object.entries(cat)
      .filter(([, v]) => Array.isArray(v))
      .map(([k, v]) => h("div", { style: "margin-bottom:7px" },
        h("div", { class: "l", style: "font:600 10.5px/1.4 var(--mono);letter-spacing:.08em;" +
          "text-transform:uppercase;color:var(--fg-faint)" },
          (BUCKET[k] || k.replace(/_/g, " ")) + " \u00b7 " + v.length),
        v.length
          ? h("div", { class: "refs" }, v.map((r) => h("span", { class: "ref" }, r)))
          : h("span", { class: "no-ev" }, "none")));

    const layers = cat.layers
      ? table(["Catalog layer", "Entries"], Object.entries(cat.layers).sort().map(([k, v]) =>
          h("tr", {}, h("td", { class: "m" }, k), h("td", { class: "num" }, v))))
      : null;

    return panel("Catalog coverage", {
      count: cat.considered !== undefined && pct !== null
        ? pct + "% of " + plural(cat.considered, "changed component")
        : null,
      extra: h("span", { class: "count" },
        plural(cat.parts || cat.parts_in_catalog || 0, "entry", "entries") + " loaded"),
    },
      h("p", { class: "fdetail", style: "margin:0 0 9px" },
        "A rule-based finding can only fire on a component the catalog knows. " +
        (cat.reviewed !== undefined && cat.parts
          ? cat.reviewed + " of " + cat.parts + " catalog entries have been checked against " +
            "a datasheet by a human, so treat the rest as prompts to verify, not conclusions."
          : "Entries that nobody has checked against a datasheet are prompts to verify.")),
      h("div", {}, buckets),
      layers);
  }

  function findingRow(f) {
    return h("article", { class: "finding " + f.confidence_class + " sev-" + f.severity },
      h("div", { class: "fhead" },
        schip(f.severity),
        h("span", { class: "fid" }, f.id),
        h("span", { class: "ftitle" }, f.title),
        cchip(f.confidence_class)),
      f.detail ? h("p", { class: "fdetail" }, f.detail) : null,
      refChips(f.refs, traceRef),
      evidenceBlock(f.evidence));
  }

  function retestPanel(rep) {
    const rows = (rep.required_retests || []).map((t) =>
      h("tr", {},
        h("td", { class: "m" }, t.name),
        h("td", {}, cchip(t.confidence_class)),
        h("td", {}, t.reason, evidenceBlock(t.evidence, "Evidence"))));
    return panel("Required re-tests", { count: (rep.required_retests || []).length, flush: true },
      rows.length ? table(["Test", "Confidence", "Why"], rows) : empty("None."));
  }

  function traceRef(ref) {
    S.lineage.ref = ref;
    S.lineage.rev = S.cmp.b || "HEAD";
    location.hash = "#/lineage";
    if (S.view === "lineage") runLineage();
  }

  /* -------------------------------------------------------------- lineage */

  function lineageView(main) {
    const refInput = h("input", { id: "ln-ref", list: "ln-refs", value: S.lineage.ref || "",
      size: 12, spellcheck: "false", placeholder: exampleRef() || "component",
      oninput: (e) => { S.lineage.ref = e.target.value.trim(); } });
    const opts = [{ value: "HEAD", label: "HEAD" }].concat(revOptions());
    const bar = h("div", { class: "toolbar" },
      field("Component ref", refInput),
      field("Revision", select("ln-rev", opts, S.lineage.rev,
        (e) => { S.lineage.rev = e.target.value; })),
      field("Hops", select("ln-depth",
        [1, 2, 3].map((d) => ({ value: d, label: String(d) })), S.lineage.depth,
        (e) => { S.lineage.depth = Number(e.target.value); })),
      h("button", { class: "primary", type: "button", id: "ln-go", onclick: runLineage }, "Trace"),
      h("datalist", { id: "ln-refs" }));

    const out = h("div", { id: "ln-out" }, loading("Tracing"));
    mount(main,
      h("div", { class: "view-head" },
        h("h1", {}, "Device lineage"),
        h("p", {}, "Start from one component reference and walk outwards: shared connections, " +
          "dependent firmware and tests, and — the part nobody else answers — the " +
          "physical units already built with it, and which variant each one carries.")),
      panel("Trace a component", {}, bar),
      out);
    S.ui.lnOut = out;
    runLineage();
  }

  function fillRefList(refs) {
    const dl = byId("ln-refs");
    if (!dl) return;
    clear(dl);
    Array.from(new Set(refs)).sort().forEach((r) => dl.append(h("option", { value: r })));
  }

  async function runLineage() {
    const out = S.ui.lnOut;
    if (!out) return;
    const ref = S.lineage.ref;
    if (!ref) {
      const eg = exampleRef();
      clear(out).append(empty("Enter a component reference" +
        (eg ? ", for example " + eg + "." : ".")));
      return;
    }
    clear(out).append(loading("Tracing " + ref));
    const go = byId("ln-go");
    if (go) go.disabled = true;
    try {
      const q = "component=" + encodeURIComponent(ref) + "&rev=" +
        encodeURIComponent(S.lineage.rev) + "&depth=" + S.lineage.depth;
      const [graph, fleet] = await Promise.all([
        api("/api/graph/affected?" + q),
        api("/api/devices/affected?component=" + encodeURIComponent(ref)).catch(() => []),
      ]);
      fillRefList([ref].concat(graph.components.map((c) => c.ref)));
      mount(clear(out),
        lineageStats(ref, graph, fleet),
        fleetPanel(ref, fleet),
        h("div", { class: "grid-halves" }, connectedPanel(graph), dependentsPanel(graph)),
        h("div", { id: "ln-passport" }, S.lineage.serial ? loading("Loading passport") : null));
      if (S.lineage.serial) showPassport(S.lineage.serial);
    } catch (e) {
      clear(out).append(errBox(e));
      if (e.message.indexOf("404") >= 0) {
        out.append(empty("No component " + ref + " in revision " + S.lineage.rev + "."));
      }
    } finally {
      if (go) go.disabled = false;
    }
  }

  function lineageStats(ref, g, fleet) {
    return h("div", { class: "stats" },
      stat(ref, "component"),
      stat(g.nets.length, "connections"),
      stat(g.components.length, "components"),
      stat(g.firmware.length, "firmware files"),
      stat(g.tests.length, "tests"),
      stat(g.devices.length, "units at this rev"),
      stat(fleet.length, "units carrying " + ref));
  }

  function fleetPanel(ref, fleet) {
    if (!fleet.length) {
      return panel("Built units carrying " + ref, { count: 0 },
        emptyArt("rack", "No registered unit carries " + ref + " yet. Register one against a release with " +
          "`vcm device register SERIAL --release NAME`, then trace again."));
    }
    const groups = new Map();
    for (const d of fleet) {
      const k = (d.value || "?") + "\u0000" + (d.mpn || "") + "\u0000" + (d.footprint || "");
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(d);
    }
    const blocks = [];
    for (const [k, list] of groups) {
      const [value, mpn, fp] = k.split("\u0000");
      blocks.push(h("div", { style: "margin-bottom:14px" },
        h("div", { class: "toolbar", style: "margin-bottom:7px" },
          h("span", { class: "ref", style: "font-size:12px;padding:3px 7px" }, value),
          h("span", { class: "count mono", style: "color:var(--fg-faint);font-size:11px" },
            [mpn, fp].filter(Boolean).join("  ·  ")),
          h("span", { class: "count mono", style: "color:var(--fg-dim)" },
            plural(list.length, "unit"))),
        h("div", { class: "devgrid" }, list.map((d) => deviceCard(d)))));
    }
    return panel("Built units carrying " + ref, {
      count: plural(fleet.length, "unit"),
      extra: h("span", { class: "count" }, "grouped by the component actually fitted"),
    }, blocks);
  }

  function deviceCard(d) {
    const sel = S.lineage.serial === d.serial;
    return h("button", {
      class: "devcard", type: "button", "aria-pressed": sel ? "true" : "false",
      onclick: () => { S.lineage.serial = d.serial; showPassport(d.serial); },
    },
      h("div", { class: "sn" }, d.serial),
      h("div", { class: "meta" },
        "release ", d.release, h("br"),
        "batch ", d.batch || "—", h("br"),
        "rev ", revShort(d.revision)));
  }

  function connectedPanel(g) {
    const maxHop = Math.max(1, ...g.components.map((c) => c.hop));
    return panel("Connected components", {
      count: plural(g.components.length, "component"),
      extra: h("span", { class: "count" }, "via " + g.nets.join(", ")),
      flush: true,
    }, table(["Ref", "Hop", "Via", "Path"], g.components.map((c) =>
      h("tr", {},
        h("td", { class: "m" },
          h("button", { class: "ref", type: "button",
            onclick: () => { S.lineage.ref = c.ref; const i = byId("ln-ref");
              if (i) i.value = c.ref; runLineage(); } }, c.ref)),
        h("td", { class: "num" }, c.hop, " ",
          h("span", { class: "hopbar", style: "width:" + (26 / maxHop) * c.hop + "px" })),
        h("td", { class: "m" }, (c.via_nets || []).join(", ")),
        h("td", {}, pathChain(c.path))))));
  }

  function pathChain(path) {
    if (!path) return null;
    const out = [];
    path.forEach((node, i) => {
      const [kind, ...rest] = node.split(":");
      const name = rest.join(":");
      if (i) out.push(h("span", { class: "sep" }, "›"));
      out.push(h("span", { class: "node" + (kind === "net" ? " net" : "") }, name));
    });
    return h("div", { class: "chain" }, out);
  }

  function dependentsPanel(g) {
    const rows = [
      g.firmware.map((f) => ["firmware", f.node.replace(/^firmware:/, ""),
        (f.via || []).map((v) => v.replace(/^component:/, "")).join(", ")]),
      g.tests.map((t) => ["test", t.node.replace(/^test:/, ""),
        (t.via || []).map((v) => v.replace(/^component:/, "")).join(", ")]),
    ].flat();
    return h("div", {},
      panel("Dependent artefacts", { count: rows.length, flush: true },
        rows.length
          ? table(["Kind", "Node", "Linked via"], rows.map((r) =>
              h("tr", {}, h("td", { class: "m" }, r[0]), h("td", { class: "m" }, r[1]),
                h("td", { class: "m", style: "color:var(--fg-faint)" }, r[2]))))
          : empty("Nothing declared against these components.")),
      panel("Form factors in the blast radius", { count: g.footprints.length },
        h("div", { class: "refs" }, g.footprints.map((f) =>
          h("span", { class: "ref" }, f.replace(/^footprint:/, ""))))));
  }

  async function showPassport(serial) {
    const box = byId("ln-passport");
    if (!box) return;
    clear(box).append(loading("Loading passport for " + serial));
    try {
      const p = await api("/api/devices/" + encodeURIComponent(serial) + "/passport");
      clear(box).append(passportPanel(p));
      box.scrollIntoView({ block: "nearest" });
    } catch (e) {
      clear(box).append(errBox(e));
    }
  }

  function passportPanel(p) {
    const kv = h("dl", { class: "kv" },
      h("dt", {}, "Serial"), h("dd", {}, p.serial),
      h("dt", {}, "Batch"), h("dd", {}, p.batch || "—"),
      h("dt", {}, "Registered"), h("dd", {}, when(p.registered_at)),
      h("dt", {}, "Release"), h("dd", {}, p.release.name),
      h("dt", {}, "Manifest"), h("dd", { title: p.release.manifest_sha256 },
        shortHash(p.release.manifest_sha256, 32)),
      h("dt", {}, "Revision"), h("dd", { title: p.revision.id },
        shortHash(p.revision.id, 20) + "  " + (p.revision.tags || []).join(", ")),
      h("dt", {}, "Commit"), h("dd", {}, p.revision.message),
      h("dt", {}, "Author"), h("dd", {}, p.revision.author + "  " + when(p.revision.timestamp)),
      h("dt", {}, "QR payload"), h("dd", {}, p.qr.payload));

    const lineage = h("ol", { class: "timeline" }, (p.lineage || []).map((r) =>
      h("li", {}, h("div", { class: "msg" }, r.message),
        h("div", { class: "sub" }, shortHash(r.id, 12) + "  ·  " + when(r.timestamp)))));

    const bom = table(["Ref", "Value", "Part number", "Form factor"], (p.bom || []).map((c) =>
      h("tr", {}, h("td", { class: "m" }, c.ref), h("td", { class: "m" }, c.value),
        h("td", { class: "m" }, c.mpn || "—"),
        h("td", { class: "m", style: "color:var(--fg-faint)" }, c.footprint))));

    const fw = table(["File", "sha256"], (p.firmware || []).map((f) =>
      h("tr", {}, h("td", { class: "m" }, f.path),
        h("td", { class: "m", title: f.hash }, shortHash(f.hash, 24)))));

    return panel("Passport · " + p.serial, {
      extra: h("span", { class: "count" }, "validation ", statusChip(p.validation.status)),
    },
      h("div", { class: "passport" },
        h("div", {},
          h("img", { class: "qr", alt: "QR code encoding " + p.qr.payload,
            src: "/api/devices/" + encodeURIComponent(p.serial) + "/qr.svg" }),
          h("p", { class: "empty", style: "font-size:11px" },
            "Scans to the unit's passport URL.")),
        h("div", {}, kv,
          h("h3", { style: "margin:14px 0 6px;font:600 10.5px/1 var(--mono);" +
            "letter-spacing:.09em;text-transform:uppercase;color:var(--fg-faint)" },
            "Revision lineage"), lineage)),
      h("div", { class: "grid-halves", style: "margin-top:14px" },
        panel("As-built inventory", { count: (p.bom || []).length, flush: true }, bom),
        panel("Firmware shipped", { count: (p.firmware || []).length, flush: true }, fw)));
  }

  /* ------------------------------------------------------------- releases */

  function releasesView(main) {
    const out = h("div", { id: "rl-out" }, loading("Loading releases"));
    const roll = h("div", { id: "rb-out" });
    const opts = revOptions();
    mount(main,
      h("div", { class: "view-head" },
        h("h1", {}, "Releases and rollback"),
        h("p", {}, "A release pins every artefact by hash. Verification re-hashes the object " +
          "store, which proves the manifest is internally consistent; only a signature by a key " +
          "in trustdb.json proves who vouched for it. Unsigned releases are marked as such.")),
      out,
      panel("Rollback planner", {}, h("div", { class: "toolbar" },
        field("Current", select("rb-cur", opts, S.cmp.b, null)),
        field("Roll back to", select("rb-tgt", opts, S.cmp.a, null)),
        h("button", { class: "primary", type: "button", onclick: runRollback }, "Plan rollback")),
        roll));
    S.ui.rlOut = out;
    S.ui.rbOut = roll;
    loadReleases();
  }

  async function loadReleases() {
    const out = S.ui.rlOut;
    try {
      const rels = await api("/api/releases");
      const units = {};
      S.devices.forEach((d) => { units[d.release] = (units[d.release] || 0) + 1; });
      clear(out).append(panel("Releases", { count: rels.length, flush: !!rels.length },
        !rels.length ? emptyArt("release", "No releases yet. Pin a revision with " +
          "`vcm release create NAME --rev REV`, and it will appear here to verify and roll back.") :
        table(["Name", "Revision", "Created", "Validation", "manifest sha256", "Units", ""],
          rels.map((r) => h("tr", {},
            h("td", { class: "m" }, r.name),
            h("td", {}, h("div", { class: "mono" }, shortHash(r.revision, 10)),
              h("div", { style: "color:var(--fg-faint)" }, r.revision_message)),
            h("td", { class: "m" }, when(r.created_at)),
            h("td", {}, statusChip(r.validation ? r.validation.status : "unknown")),
            h("td", { class: "m", title: r.manifest_sha256 }, shortHash(r.manifest_sha256, 20)),
            h("td", { class: "num" }, units[r.name] || 0),
            h("td", {}, h("div", { class: "toolbar" },
              h("button", { class: "ghost", type: "button",
                onclick: () => showRelease(r, true) }, "Manifest"),
              h("button", { class: "ghost", type: "button",
                onclick: (e) => verifyRelease(r.name, e.target) }, "Verify")))))),
        h("div", { id: "rl-detail" })));
      if (rels.length) showRelease(rels[rels.length - 1]);
      runRollback();
    } catch (e) {
      clear(out).append(errBox(e));
    }
  }

  function showRelease(r, scroll) {
    const box = byId("rl-detail");
    if (!box) return;
    const rows = Object.entries(r.artifacts).sort().map(([path, a]) =>
      h("tr", {}, h("td", { class: "m" }, path), h("td", { class: "m" }, a.category),
        h("td", { class: "num" }, bytes(a.size)),
        h("td", { class: "m", title: a.hash }, shortHash(a.hash, 24))));
    clear(box).append(panel("Manifest · " + r.name, {
      count: Object.keys(r.artifacts).length + " artefacts",
      extra: h("span", { class: "count" }, "schema " + r.schema),
    },
      h("div", { class: "grid-halves" },
        h("dl", { class: "kv" },
          h("dt", {}, "Revision"), h("dd", { title: r.revision }, shortHash(r.revision, 24)),
          h("dt", {}, "Parents"), h("dd", {}, (r.parents || []).map((p) =>
            shortHash(p, 12)).join(", ") || "—"),
          h("dt", {}, "Created"), h("dd", {}, when(r.created_at)),
          h("dt", {}, "Manifest"), h("dd", { title: r.manifest_sha256 }, r.manifest_sha256),
          h("dt", {}, "Signature"), (r.signatures && r.signatures.length)
            ? h("dd", {}, r.signatures.map((x) => x.signer + " (" + x.key_id + ")").join(", ") +
                "  — run Verify to check against trustdb.json")
            : h("dd", { style: "color:var(--fg-faint)" }, "unsigned"),
          r.policy ? h("dt", {}, "Policy") : null,
          r.policy ? h("dd", {}, !r.policy.present ? "no vcm-policy.json"
            : r.policy.ok ? "passed"
            : "OVERRIDDEN (" + r.policy.violations.join(", ") + "): " +
              ((r.policy.override || {}).reason || "")) : null),
        h("dl", { class: "kv" },
          h("dt", {}, "vcm"), h("dd", {}, r.toolchain.vcm),
          h("dt", {}, "python"), h("dd", {}, r.toolchain.python),
          h("dt", {}, "platform"), h("dd", {}, r.toolchain.platform),
          h("dt", {}, "kicad-cli"), h("dd", {}, r.toolchain.kicad_cli),
          h("dt", {}, "validation"), h("dd", {},
            r.validation.status + "  (" + r.validation.counts.error + " err, " +
            r.validation.counts.warning + " warn)"),
          h("dt", {}, "evidence"), h("dd", { title: r.validation.evidence_hash },
            shortHash(r.validation.evidence_hash, 20)))),
      h("div", { style: "margin-top:12px" },
        table(["Path", "Category", "Size", "sha256"], rows))));
    if (scroll) box.scrollIntoView({ block: "nearest" });
  }

  async function verifyRelease(name, btn) {
    const box = byId("rl-detail");
    if (!box) return;
    if (btn) btn.disabled = true;
    clear(box).append(loading("Verifying " + name));
    try {
      const v = await api("/api/releases/" + encodeURIComponent(name) + "/verify");
      clear(box).append(panel("Verification · " + name, {
        extra: h("span", { class: "statuschip " + (v.ok ? "ok" : "fail") },
          v.ok ? "all checks passed" : plural(v.problems.length, "problem")),
      },
        h("ul", { class: "checks" }, v.checks.map((c) =>
          h("li", { class: c.ok ? "ok" : "bad" },
            h("span", { class: "mark" }, c.ok ? "✓" : "✗"),
            h("span", {}, c.check),
            c.detail ? h("span", { class: "d" }, "  " + c.detail) : null)))));
      box.scrollIntoView({ block: "nearest" });
    } catch (e) {
      clear(box).append(errBox(e));
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function runRollback() {
    const box = S.ui.rbOut;
    const cur = byId("rb-cur").value, tgt = byId("rb-tgt").value;
    clear(box).append(loading("Planning"));
    try {
      const p = await api("/api/rollback?target=" + encodeURIComponent(tgt) +
        "&current=" + encodeURIComponent(cur));
      const bucket = (cls, title, items) => h("div", { class: "bucket " + cls },
        h("h3", {}, title + " · " + items.length),
        items.length
          ? h("ul", {}, items.map((i) => h("li", {},
              h("b", { class: "mono" }, i.ref + " "), i.instruction)))
          : h("p", { class: "empty", style: "margin:0;padding:0" }, "none"));
      mount(clear(box),
        h("div", { class: "verdict " + (p.fully_software_reversible ? "clean" : "critical"),
          style: "margin-top:12px" },
          h("div", {},
            h("div", { class: "vtitle" }, p.fully_software_reversible
              ? "Fully reversible in software"
              : (p.respin_required || []).length
                ? "Not reversible in software — " + (p.vocabulary || "platform change required")
                : "Not reversible in software — physical rework is required"),
            h("div", { class: "vdetail" }, revShort(cur) + " → " + revShort(tgt) + ". " +
              plural(p.devices_not_at_target.length, "built unit") + " not at the target " +
              "revision."),
            p.devices_not_at_target.length
              ? h("div", { class: "refs" }, p.devices_not_at_target.map((d) =>
                  h("span", { class: "ref" }, d)))
              : null)),
        h("div", { class: "buckets" },
          bucket("soft", "Software reversible", p.software_reversible),
          bucket("rework", "Physical rework", p.physical_rework),
          bucket("respin", p.vocabulary || "Platform change required", p.respin_required)),
        p.other && p.other.length
          ? panel("Other", { count: p.other.length },
              h("ul", {}, p.other.map((o) => h("li", {}, o.instruction || JSON.stringify(o)))))
          : null);
    } catch (e) {
      clear(box).append(errBox(e));
    }
  }


  /* --------------------------------------------------------------- agents */

  /* Agents propose; people decide. This page only *shows* proposals and forwards a person's
     decision to the API, which refuses an agent, the proposer, and any unauthenticated caller.
     Everything an agent wrote (rationale, arguments) is rendered as text, never as markup. */

  const MCP_SNIPPET = '{"mcpServers": {"vcm": {"command": "vcm", "args": ["-C", "/path/to/repo", "mcp"]}}}';

  function agentsView(main) {
    const out = h("div", { id: "ag-out" }, loading("Loading proposals"));
    const filter = select("ag-filter", [
      { value: "pending", label: "Pending" }, { value: "", label: "All" },
      { value: "approved", label: "Approved" }, { value: "rejected", label: "Rejected" },
    ], "pending", () => loadProposals());
    filter.setAttribute("aria-label", "Filter proposals by status");
    mount(main,
      h("div", { class: "view-head" },
        h("h1", {}, "Agents"),
        h("p", {}, "An AI agent can read everything VCM knows and can only propose a change. " +
          "Nothing below happens until a named person approves it here. An agent can never " +
          "approve or reject, and nobody can decide their own proposal.")),
      out);
    S.ui.agOut = out;
    S.ui.agFilter = filter;
    loadProposals();
  }

  async function loadProposals() {
    const out = S.ui.agOut;
    try {
      const all = await api("/api/proposals");
      const want = S.ui.agFilter.value;
      const shown = all.filter((p) => !want || p.status === want)
        .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
      const n = (st) => all.filter((p) => p.status === st).length;
      clear(out).append(
        h("div", { class: "stats" },
          stat(n("pending"), "pending", n("pending") ? "bad" : null),
          stat(n("approved"), "approved"),
          stat(n("rejected"), "rejected")),
        panel("Proposals", { count: shown.length, extra: S.ui.agFilter },
          shown.length
            ? h("div", {}, shown.map(proposalCard))
            : all.length
              ? empty("Nothing with that status.")
              : emptyArt("agent", "No proposals yet. When an agent connected over MCP asks for a " +
                  "change, it appears here with its reason, for a person to decide.")),
        panel("Connect an agent", {},
          h("p", { class: "empty", style: "margin:0 0 10px" },
            "VCM serves an MCP server over stdio. It is offline, needs no API key and opens no " +
            "sockets. Add it to your MCP client's config, for example `.mcp.json`:"),
          h("pre", { class: "mono", style: "margin:0;overflow-x:auto" }, MCP_SNIPPET),
          h("p", { class: "empty", style: "margin:10px 0 0" },
            "Read tools cannot change anything. The only write is a proposal. Signing, catalog " +
            "reviews, policy overrides and pushing are not available to an agent at all. Not yet " +
            "driven by a live agent client: it has been tested over the protocol only.")));
    } catch (e) {
      clear(out).append(errBox(e));
    }
  }

  function proposalCard(p) {
    const isAgent = String(p.proposer || "").startsWith("agent:");
    const reason = h("input", { type: "text", placeholder: "reason (optional)",
      "aria-label": "Reason for rejecting " + p.id, style: "min-width:200px" });
    const msg = h("p", { class: "err", style: "margin:8px 0 0", hidden: true });
    const decide = async (what, btn) => {
      btn.disabled = true; msg.hidden = true;
      try {
        await api("/api/proposals/" + p.id + "/" + what,
          { json: what === "reject" ? { reason: reason.value.trim() } : {} });
        loadProposals();
      } catch (err) {
        msg.textContent = err.message; msg.hidden = false; btn.disabled = false;
      }
    };
    const args = Object.keys(p.args || {}).sort().map((k) => {
      const v = String(p.args[k]);
      return h("span", { class: "mono", title: v, style: "margin-right:14px;display:inline-block" },
        k + "=" + (/^[0-9a-f]{40,}$/.test(v) ? shortHash(v, 12) : v));
    });
    const pending = p.status === "pending";
    return h("div", { class: "proposal", style: "padding:14px 0;border-top:1px solid var(--line)" },
      h("div", { class: "vmeta", style: "display:flex;gap:10px;flex-wrap:wrap;align-items:center" },
        h("b", { class: "mono" }, p.id),
        h("span", { class: "mono", style: "border:1px solid var(--line-2);border-radius:999px;" +
          "padding:2px 10px;font-size:11px;letter-spacing:.06em;text-transform:uppercase" },
          p.status),
        h("span", { class: "mono" }, p.action),
        h("span", { class: "count mono", title: p.proposer },
          (isAgent ? "agent " : "") + "proposed by " + p.proposer + " · " + when(p.created_at))),
      h("div", { style: "margin:6px 0" }, p.summary),
      h("blockquote", { style: "margin:6px 0;padding-left:12px;border-left:2px solid var(--line-2);" +
        "color:var(--fg-dim)" }, p.rationale),
      h("div", {}, args),
      !pending
        ? h("p", { class: "empty", style: "margin:8px 0 0" },
            p.status + " by " + (p.decided_by || "?") + " · " + when(p.decided_at) +
            (p.reason ? " · " + p.reason : ""))
        : h("div", { class: "toolbar", style: "margin-top:10px" },
            h("button", { class: "primary", type: "button",
              onclick: (e) => decide("approve", e.target) }, "Approve"),
            reason,
            h("button", { type: "button", onclick: (e) => decide("reject", e.target) }, "Reject")),
      msg);
  }

  /* --------------------------------------------------------------- review */

  function reviewView(main) {
    const out = h("div", { id: "pr-out" }, loading("Loading pull requests"));
    mount(main,
      h("div", { class: "view-head" },
        h("h1", {}, "Hardware pull requests"),
        h("p", {}, "A local review record attached to a revision pair. There is no merge " +
          "action. With API tokens, authors and approvers are the token's user and " +
          "self-approval is refused; on a server started with --insecure they are " +
          "self-asserted free text.")),
      out);
    S.ui.prOut = out;
    loadPrs();
  }

  async function loadPrs() {
    const out = S.ui.prOut;
    try {
      const prs = await api("/api/prs");
      clear(out).append(panel("Open reviews", { count: prs.length, flush: true },
        prs.length
          ? table(["#", "Title", "Base → Head", "State", "Approvals", ""], prs.map((p) =>
              h("tr", {}, h("td", { class: "m" }, p.id), h("td", {}, p.title),
                h("td", { class: "m" }, p.base + " → " + p.head),
                h("td", {}, statusChip(p.state)),
                h("td", { class: "num" }, p.approvals),
                h("td", {}, h("button", { class: "ghost", type: "button",
                  onclick: () => showPr(p.id) }, "Open")))))
          : emptyArt("review", "No pull requests yet. Open one with " +
            "`vcm pr open --base REV --head REV --title TEXT`; a person then approves it here.")),
        h("div", { id: "pr-detail" }));
      if (S.review.selected) showPr(S.review.selected);
      else if (prs.length) showPr(prs[0].id);
    } catch (e) {
      clear(out).append(errBox(e));
    }
  }

  async function showPr(id) {
    S.review.selected = id;
    const box = byId("pr-detail");
    if (!box) return;
    clear(box).append(loading("Loading PR #" + id));
    try {
      const p = await api("/api/prs/" + id);
      const st = p.status;
      const by = p.impact && p.impact.summary ? p.impact.summary.by_class : {};
      const crit = (p.impact ? p.impact.findings : [])
        .filter((f) => st.critical_findings.indexOf(f.id) >= 0);

      const comment = h("textarea", { id: "pr-text", rows: 2, placeholder: "Leave a note",
        style: "flex:1 1 240px" });
      const author = h("input", { id: "pr-author", placeholder: "your name", size: 12 });

      clear(box).append(panel("PR #" + p.id + " · " + p.title, {
        extra: h("span", { class: "count" },
          "by " + p.author + " · " + when(p.created_at)),
      },
        h("div", { class: "verdict " + (crit.length ? "critical" : "clean") },
          h("div", {},
            h("div", { class: "vtitle" }, st.ready_for_merge
              ? "Approved — but " + plural(st.critical_findings.length, "critical finding") +
                " still open"
              : "Not ready: " + plural(st.approvals, "approval")),
            crit.length
              ? h("div", { class: "vdetail" }, crit.map((f) =>
                  h("div", {}, f.id + " · " + f.title)))
              : h("div", { class: "vdetail" }, "No critical findings."),
            h("div", { class: "vmeta" },
              schip(crit.length ? "critical" : "info"),
              st.validation_regression ? statusChip("fail") : statusChip("pass"),
              h("span", { class: "count mono" }, "validation regression: " +
                st.validation_regression),
              CONF_ORDER.map((c) => h("span", { class: "cchip " + c }, cmark(c),
                (by[c] || 0) + " " + CONF[c].label))))),
        h("div", { class: "grid-halves", style: "margin-top:12px" },
          h("div", {},
            h("dl", { class: "kv" },
              h("dt", {}, "Base"), h("dd", { title: p.base }, shortHash(p.base, 16)),
              h("dt", {}, "Head"), h("dd", { title: p.head }, shortHash(p.head, 16)),
              h("dt", {}, "State"), h("dd", {}, p.state),
              h("dt", {}, "Approvals"), h("dd", {}, (p.approvals || []).map((a) =>
                a.by + " (" + when(a.at) + ")").join(", ") || "none")),
            h("div", { class: "toolbar", style: "margin-top:12px" },
              h("button", { class: "primary", type: "button", onclick: () => {
                S.cmp.a = p.base; S.cmp.b = p.head; location.hash = "#/compare";
              } }, "Open in Compare"),
              h("button", { type: "button", onclick: async (e) => {
                const who = author.value.trim();
                if (!who) { author.focus(); return; }
                e.target.disabled = true;
                try { await api("/api/prs/" + p.id + "/approvals", { json: { by: who } }); }
                catch (err) { alert(err.message); }
                e.target.disabled = false;
                showPr(p.id);
              } }, "Approve as"),
              author)),
          h("div", {},
            h("h3", { style: "margin:0 0 4px;font:600 10.5px/1 var(--mono);letter-spacing:.09em;" +
              "text-transform:uppercase;color:var(--fg-faint)" },
              "Comments · " + p.comments.length),
            p.comments.length
              ? h("div", {}, p.comments.map((c) => h("div", { class: "comment" },
                  h("div", { class: "who" }, c.author + "  ·  " + when(c.at)),
                  h("div", {}, c.text))))
              : empty("No comments."),
            h("div", { class: "toolbar", style: "margin-top:8px" }, comment,
              h("button", { type: "button", onclick: async (e) => {
                const txt = comment.value.trim();
                if (!txt) { comment.focus(); return; }
                e.target.disabled = true;
                try {
                  await api("/api/prs/" + p.id + "/comments",
                    { json: { author: author.value.trim() || "unknown", text: txt } });
                } catch (err) { alert(err.message); }
                e.target.disabled = false;
                showPr(p.id);
              } }, "Comment"))))));
    } catch (e) {
      clear(box).append(errBox(e));
    }
  }

  /* -------------------------------------------------------------- history */

  function historyView(main) {
    const items = S.revs.map((r) => h("li", {},
      h("div", { class: "toolbar", style: "gap:7px" },
        h("span", { class: "msg" }, r.message),
        r.tags.map((t) => h("span", { class: "tag" }, t)),
        r.validation ? statusChip(r.validation) : null),
      h("div", { class: "sub" },
        r.short + "  ·  " + r.author + "  ·  " + when(r.timestamp) +
        "  ·  " + plural(r.artifact_count, "artefact") +
        (r.releases && r.releases.length ? "  ·  releases " + r.releases.join(", ") : "") +
        (r.devices ? "  ·  " + plural(r.devices, "built unit") : "")),
      h("div", { class: "refs" },
        Object.entries(r.changes || {}).map(([k, v]) =>
          h("span", { class: "ref" }, k.replace(/_/g, " ") + " " + v))),
      refChips(r.changed_components, traceRef),
      h("div", { class: "toolbar", style: "margin-top:7px" },
        h("button", { class: "ghost", type: "button",
          onclick: () => { S.cmp.a = r.id; location.hash = "#/compare"; } }, "Set as base"),
        h("button", { class: "ghost", type: "button",
          onclick: () => { S.cmp.b = r.id; location.hash = "#/compare"; } }, "Set as head"))));

    const graphBox = h("div", { id: "gs-out" }, loading("Loading graph"));
    mount(main,
      h("div", { class: "view-head" },
        h("h1", {}, "History"),
        h("p", {}, "First-parent revision history. No merges: remotes sync fast-forward only.")),
      panel("Timeline", { count: plural(S.revs.length, "revision"), flush: true },
        h("ol", { class: "timeline" }, items)),
      graphBox);
    loadGraphStats(graphBox);
  }

  async function loadGraphStats(box) {
    try {
      const g = await api("/api/graph/stats");
      const cell = (obj) => h("div", { class: "stats" },
        Object.entries(obj).sort().map(([k, v]) => stat(v, k.replace(/_/g, " "))));
      clear(box).append(panel("Engineering graph", {
        extra: h("span", { class: "count" }, "backend " + g.backend),
      }, h("h3", { style: "margin:0 0 6px;font:600 10.5px/1 var(--mono);letter-spacing:.09em;" +
        "text-transform:uppercase;color:var(--fg-faint)" }, "Nodes"), cell(g.nodes),
        h("h3", { style: "margin:12px 0 6px;font:600 10.5px/1 var(--mono);letter-spacing:.09em;" +
          "text-transform:uppercase;color:var(--fg-faint)" }, "Edges"), cell(g.edges)));
    } catch (e) {
      clear(box).append(errBox(e));
    }
  }

  /* ---------------------------------------------------------------- fleet */

  const RACK_TONE = { over: "over", tight: "tight", ok: "ok", "no budget": "nobudget" };
  const RACK_LABEL = { over: "over budget", tight: "over 90%", ok: "within budget",
    "no budget": "no budget set" };

  function fleetView(main) {
    const releases = h("span", {});
    const powerOut = h("div", {}, loading("Loading racks"));
    const planOut = h("div", {});
    const findOut = h("div", {});
    const summary = h("div", {});
    const revOpts = revOptions();
    const upgradeOpts = [{ value: "", label: "no upgrade (now only)" }].concat(revOpts);
    const relSelect = (id) => select(id, [{ value: "", label: "all units" }], "", null);
    const num = (id, v) => h("input", { id: id, type: "number", min: 1, value: v, style: "width:6em" });
    const txt = (id, ph) => h("input", { id: id, type: "text", placeholder: ph, size: 14 });

    mount(main,
      h("div", { class: "view-head" },
        h("h1", {}, "Fleet"),
        h("p", {}, "Every registered unit at once: how much power each rack is provisioned to " +
          "draw now and after a change, the order in which to roll the change out, and which " +
          "units carry a given part or firmware. Power here is declared draw from manifests " +
          "and the component catalog, not measured consumption.")),
      summary,
      panel("Rack power", { extra: releases },
        h("div", { class: "toolbar" },
          field("Upgrade to", select("fl-up", upgradeOpts, S.fleet.upgrade, null)),
          field("Only units on release", relSelect("fl-up-rel")),
          h("button", { class: "primary", type: "button", onclick: loadPower }, "Show")),
        powerOut),
      panel("Rollout plan", {},
        h("div", { class: "toolbar" },
          field("Move to", select("fl-to", revOpts, S.fleet.upgrade || S.cmp.b, null)),
          field("Only units on release", relSelect("fl-to-rel")),
          field("Max per rack per wave", num("fl-mpr", 1)),
          field("Wave size", num("fl-ws", 10)),
          h("button", { class: "primary", type: "button", onclick: runRollout }, "Plan rollout")),
        planOut),
      panel("Find units", {},
        h("div", { class: "toolbar" },
          field("Type", txt("fl-type", "gpu, dimm, psu")),
          field("Model contains", txt("fl-model", "A100")),
          field("Part number contains", txt("fl-mpn", "M393A4K40")),
          field("Firmware", txt("fl-fw", "gpu_driver<535")),
          field("Rack", txt("fl-rack", "")),
          h("button", { class: "primary", type: "button", onclick: runFind }, "Find")),
        findOut));
    S.ui.fleet = { powerOut, planOut, findOut, summary, releases };
    initFleet();
  }

  async function initFleet() {
    const ui = S.ui.fleet;
    try {
      const [rels, devices] = await Promise.all([api("/api/releases"), api("/api/devices")]);
      S.devices = devices;
      for (const id of ["fl-up-rel", "fl-to-rel"]) {
        const sel = byId(id);
        if (!sel) return;
        rels.forEach((r) => sel.append(h("option", { value: r.name }, "release " + r.name)));
      }
      ui.releases.textContent = "";
      ui.releases.append(h("span", { class: "count" }, plural(rels.length, "release")));
    } catch (e) {
      clear(ui.powerOut).append(errBox(e));
      return;
    }
    await loadPower();
  }

  function fleetSummary(power) {
    const placed = S.devices.filter((d) => d.location && d.location.rack).length;
    const over = power.racks.filter((r) => r.after_status === "over").length;
    mount(clear(S.ui.fleet.summary), h("div", { class: "stats" },
      stat(S.devices.length, "units"),
      stat(power.racks.length, "racks"),
      stat(placed, "placed in a rack"),
      stat(S.devices.length - placed, "not placed", S.devices.length - placed ? "bad" : null),
      stat(over, power.upgrade_to ? "racks over budget after" : "racks over budget",
        over ? "bad" : "ok")));
  }

  function wattBars(r, scale, showAfter) {
    const pct = (w) => Math.max(0, Math.min(100, (w / scale) * 100)) + "%";
    const bar = (w, cls, label) => h("div", { class: "wbar" },
      h("span", { class: "wl" }, label),
      h("span", { class: "wtrack" },
        h("span", { class: "wfill " + cls, style: "width:" + pct(w) }),
        r.budget_w ? h("span", { class: "wbudget", style: "left:" + pct(r.budget_w),
          title: "budget " + r.budget_w + " W" }) : null),
      h("span", { class: "wv" }, Math.round(w).toLocaleString() + " W"));
    return h("div", { class: "wbars" },
      bar(r.now_w, "now " + RACK_TONE[r.now_status], "now"),
      showAfter ? bar(r.after_w, "after " + RACK_TONE[r.after_status], "after") : null);
  }

  async function loadPower() {
    const ui = S.ui.fleet;
    const up = byId("fl-up").value;
    const rel = byId("fl-up-rel").value;
    S.fleet.upgrade = up || null;
    clear(ui.powerOut).append(loading("Summing declared draw per rack"));
    try {
      const q = new URLSearchParams();
      if (up) q.set("upgrade_to", up);
      if (rel) q.set("release", rel);
      const p = await api("/api/fleet/power?" + q);
      fleetSummary(p);
      if (!p.racks.length) {
        clear(ui.powerOut).append(emptyArt("rack", "No racks yet. Define one with `vcm fleet rack define " +
          "NAME --budget W` and place units with `vcm fleet place`, or import placement " +
          "from NetBox with `vcm fleet netbox`."));
        return;
      }
      const scale = Math.max(1, ...p.racks.map((r) => Math.max(r.budget_w || 0, r.now_w,
        r.after_w))) * 1.05;
      const status = up ? "after_status" : "now_status";
      const rows = p.racks.map((r) => h("div", { class: "rackrow " + RACK_TONE[r[status]] },
        h("div", { class: "rackname" },
          h("b", { class: "mono" }, r.rack),
          h("span", { class: "rstate " + RACK_TONE[r[status]] }, RACK_LABEL[r[status]]),
          h("span", { class: "dim" }, plural(r.units.length, "unit") +
            (r.budget_w ? " · budget " + r.budget_w.toLocaleString() + " W" : ""))),
        wattBars(r, scale, !!up),
        r.unknown.length ? h("p", { class: "empty" }, "Draw unknown for " +
          r.unknown.join(", ") + "; excluded from the totals.") : null,
        refChips(r.units)));
      const over = p.over_after;
      mount(clear(ui.powerOut),
        up && over.length
          ? h("div", { class: "verdict critical" }, h("div", {},
              h("div", { class: "vtitle" }, plural(over.length, "rack") + " would exceed " +
                "the power budget after moving to " + revShort(up)),
              h("div", { class: "vdetail" }, over.join(", ") + ". " + p.note)))
          : up ? h("div", { class: "verdict clean" }, h("div", {},
              h("div", { class: "vtitle" }, "Every rack stays within budget after moving to " +
                revShort(up)),
              h("div", { class: "vdetail" }, p.note))) : null,
        h("div", { class: "racks" }, rows),
        h("p", { class: "empty" }, "The bar marker is the rack's budget. " + p.note));
    } catch (e) {
      clear(ui.powerOut).append(errBox(e));
    }
  }

  async function runRollout() {
    const ui = S.ui.fleet;
    const q = new URLSearchParams({ to: byId("fl-to").value,
      max_per_rack: byId("fl-mpr").value || "1", wave_size: byId("fl-ws").value || "10" });
    const rel = byId("fl-to-rel").value;
    if (rel) q.set("release", rel);
    clear(ui.planOut).append(loading("Running impact analysis for each starting revision"));
    try {
      const p = await api("/api/fleet/rollout?" + q);
      const tone = p.blocked.length || p.deferred.length ? "warning" : "clean";
      const waves = p.waves.map((w) => h("div", { class: "wave" },
        h("h3", {}, "Wave " + w.wave, h("span", { class: "count" },
          plural(w.units.length, "unit"))),
        h("ul", {}, w.units.map((u) => h("li", {},
          h("b", { class: "mono" }, u.serial), h("span", { class: "dim" }, "  " + u.rack))))));
      mount(clear(ui.planOut),
        h("div", { class: "verdict " + tone, style: "margin-top:12px" }, h("div", {},
          h("div", { class: "vtitle" }, plural(p.scheduled, "unit") + " scheduled in " +
            plural(p.waves.length, "wave") + " to " + revShort(p.to)),
          h("div", { class: "vdetail" }, plural(p.blocked.length, "unit") + " held back by " +
            "critical findings, " + plural(p.deferred.length, "unit") + " deferred because " +
            "the rack would exceed its power budget. At most " + p.max_per_rack +
            " per rack per wave."))),
        waves.length ? h("div", { class: "waves" }, waves) : null,
        p.blocked.length ? panel("Held back: critical findings", { count: p.blocked.length },
          h("ul", { class: "held" }, p.blocked.map((b) => h("li", {},
            h("b", { class: "mono" }, b.serial),
            h("span", { class: "dim" }, "  from " + revShort(b.from)),
            h("ul", {}, b.critical.map((c) => h("li", {}, schip("critical"), " ", c))))))) : null,
        p.deferred.length ? panel("Deferred: rack power", { count: p.deferred.length },
          h("ul", { class: "held" }, p.deferred.map((d) => h("li", {},
            h("b", { class: "mono" }, d.serial), h("span", { class: "dim" }, "  " + d.rack +
              ": " + d.reason))))) : null,
        !p.waves.length && !p.blocked.length && !p.deferred.length
          ? empty("Every unit in scope is already at " + revShort(p.to) + ".") : null);
    } catch (e) {
      clear(ui.planOut).append(errBox(e));
    }
  }

  /* Eight identical accelerators read as one line, not eight chips. */
  function groupByModel(components) {
    const groups = new Map();
    components.forEach((c) => {
      const k = c.model || c.mpn || c.ref;
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(c.ref);
    });
    return [...groups.entries()];
  }

  function unitsCsv(units) {
    const esc = (v) => '"' + String(v === null || v === undefined ? "" : v).replace(/"/g, '""') + '"';
    const lines = [["serial", "rack", "position", "release", "revision", "components", "firmware"]
      .join(",")];
    units.forEach((u) => lines.push([u.serial, u.rack, u.position, u.release,
      shortHash(u.revision, 12), u.components.map((c) => c.ref + "=" + c.model).join(" "),
      Object.entries(u.firmware || {}).map(([k, v]) => k + "=" + v).join(" ")].map(esc).join(",")));
    return lines.join("\n") + "\n";
  }

  async function runFind() {
    const ui = S.ui.fleet;
    const q = new URLSearchParams();
    const map = { type: "fl-type", model: "fl-model", mpn: "fl-mpn", rack: "fl-rack" };
    for (const [k, id] of Object.entries(map)) {
      const v = byId(id).value.trim();
      if (v) q.set(k, v);
    }
    byId("fl-fw").value.split(/[;,]/).map((x) => x.trim()).filter(Boolean)
      .forEach((f) => q.append("firmware", f));
    clear(ui.findOut).append(loading("Searching every registered unit"));
    try {
      const units = await api("/api/fleet/query?" + q);
      const byRack = {};
      units.forEach((u) => { const k = u.rack || "(not placed)"; byRack[k] = (byRack[k] || 0) + 1; });
      const comps = units.reduce((n, u) => n + u.components.length, 0);
      const download = h("button", { class: "ghost", type: "button", onclick: () => {
        const a = h("a", { href: URL.createObjectURL(new Blob([unitsCsv(units)],
          { type: "text/csv" })), download: "vcm-units.csv" });
        document.body.append(a); a.click(); a.remove();
      } }, "Download CSV");
      mount(clear(ui.findOut),
        h("div", { class: "stats", style: "margin-top:12px" },
          stat(units.length, "units"), stat(comps, "matching components"),
          stat(Object.keys(byRack).length, "racks"),
          h("div", { class: "stat" }, h("div", { class: "n txt" },
            Object.entries(byRack).map(([k, v]) => k + " " + v).join(" · ") || "—"),
            h("div", { class: "l" }, "by rack"))),
        units.length ? h("div", { class: "toolbar", style: "justify-content:flex-end" }, download)
          : null,
        units.length ? table(["Serial", "Rack", "Release", "Matching components", "Firmware"],
          units.map((u) => h("tr", {},
            h("td", { class: "m" }, u.serial),
            h("td", {}, (u.rack || "—") + (u.position ? " · U" + u.position : "")),
            h("td", {}, u.release, h("div", { class: "dim m" }, revShort(u.revision))),
            h("td", {}, u.components.length
              ? h("div", { class: "refs" }, groupByModel(u.components).map(([model, refs]) =>
                  h("span", { class: "ref", title: refs.join(", ") },
                    (refs.length > 1 ? refs.length + "× " : refs[0] + " ") + model)))
              : h("span", { class: "dim" }, "(no component filter)")),
            h("td", { class: "m" }, Object.entries(u.firmware || {}).map(([k, v]) =>
              h("div", {}, k + " " + v))))))
          : empty("No unit matches. Model and part number match as case-insensitive " +
            "substrings; firmware takes comparisons such as gpu_driver<535 (several separated " +
            "by commas)."));
    } catch (e) {
      clear(ui.findOut).append(errBox(e));
    }
  }

  /* --------------------------------------------------------------- router */

  const VIEWS = {
    compare: compareView,
    lineage: lineageView,
    releases: releasesView,
    review: reviewView,
    agents: agentsView,
    fleet: fleetView,
    history: historyView,
  };

  function route() {
    const name = (location.hash.replace(/^#\//, "") || "compare").split("?")[0];
    const view = VIEWS[name] ? name : "compare";
    S.view = view;
    document.querySelectorAll("nav.rail a").forEach((a) => {
      if (a.dataset.view === view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    const main = clear(document.getElementById("main"));
    S.ui = {};
    try {
      VIEWS[view](main);
    } catch (e) {
      main.append(errBox(e));
    }
  }

  /* ----------------------------------------------------------------- boot */

  // null is "auto": the polar look, which also follows prefers-color-scheme for light.
  const THEMES = [null, "light", "terminal"];

  function applyTheme(t) {
    if (t) document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
  }

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("vcm-theme"); } catch (e) { saved = null; }
    // ?theme=dark|light forces a theme, for demos and screenshots.
    // ?theme=auto|light|terminal forces a theme, for demos and screenshots. A missing param
    // must not count as a match: THEMES contains null, and includes(null) would be true.
    const forced = new URLSearchParams(location.search).get("theme");
    if (forced === "auto") saved = null;
    else if (forced && THEMES.includes(forced)) saved = forced;
    applyTheme(saved);
    const btn = byId("theme");
    btn.addEventListener("click", () => {
      // auto (polar) -> light -> terminal -> auto
      const cur = document.documentElement.getAttribute("data-theme");
      const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
      applyTheme(next);
      try {
        if (next) localStorage.setItem("vcm-theme", next);
        else localStorage.removeItem("vcm-theme");
      } catch (e) { /* storage unavailable; theme still applies for this page */ }
      btn.textContent = next ? "Theme: " + next : "Theme: auto";
    });
    btn.textContent = saved ? "Theme: " + saved : "Theme: auto";
  }

  function repoMeta(info, ok) {
    const box = clear(byId("repo-meta"));
    mount(box,
      h("span", {}, h("span", { class: "dot " + (ok ? "up" : "down") }), " ",
        h("span", { class: "k" }, "api "), ok ? "ok" : "down"),
      h("span", { title: info.path }, h("span", { class: "k" }, "repo "),
        String(info.path || "").replace(/\/+$/, "").split("/").pop() || info.path),
      h("span", {}, h("span", { class: "k" }, "branch "), info.branch || "detached"),
      h("span", { title: info.head }, h("span", { class: "k" }, "head "),
        shortHash(info.head, 10)),
      h("span", {}, h("span", { class: "k" }, "tags "),
        Object.keys(info.tags || {}).join(" ") || "—"));
  }

  async function boot() {
    initTheme();
    try {
      const health = await api("/api/health").catch(() => null);
      const [info, revs, devices] = await Promise.all([
        api("/api/repo"), api("/api/timeline"), api("/api/devices").catch(() => []),
      ]);
      S.repo = info;
      S.revs = revs;
      S.devices = devices;
      S.cmp.a = revs.length > 1 ? revs[1].id : (revs[0] ? revs[0].id : null);
      S.cmp.b = revs.length ? revs[0].id : null;
      S.lineage.rev = S.cmp.b || "HEAD";
      S.lineage.ref = S.lineage.ref || exampleRef();
      repoMeta(info, health && health.status === "ok");
      window.addEventListener("hashchange", route);
      route();
    } catch (e) {
      clear(byId("repo-meta")).append(
        h("span", {}, h("span", { class: "dot down" }), " api unreachable"));
      mount(clear(byId("main")),
        h("div", { class: "view-head" }, h("h1", {}, "Cannot reach the VCM API")),
        errBox(e),
        h("p", { class: "empty" },
          "Start the API against a VCM repository, for example: " +
          "VCM_REPO=/path/to/repo python -m uvicorn vcm_api.app:app"));
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
