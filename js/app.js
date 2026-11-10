/* OrgGraph — static org chart explorer.
 * Loads data/orgchart.db with sql.js (SQLite compiled to WebAssembly) and renders
 * everything client-side. No backend.
 */
(function () {
  "use strict";

  const DB_URL = "data/orgchart.db";
  const FUNCTIONS = {
    executive:        { label: "Executive",        color: "#6b7280" },
    engineering:      { label: "Engineering",      color: "#3b5bdb" },
    data:             { label: "Data",             color: "#0b8a8a" },
    security:         { label: "Security",         color: "#c2255c" },
    it:               { label: "IT",               color: "#5f6b7a" },
    product:          { label: "Product",          color: "#7048e8" },
    design:           { label: "Design",           color: "#d6336c" },
    sales:            { label: "Sales",            color: "#e8590c" },
    marketing:        { label: "Marketing",        color: "#f08c00" },
    customer_success: { label: "Customer Success", color: "#2b8a3e" },
    support:          { label: "Support",          color: "#5c940d" },
    finance:          { label: "Finance",          color: "#1971c2" },
    people:           { label: "People / HR",      color: "#ae3ec9" },
    legal:            { label: "Legal",            color: "#495057" },
    operations:       { label: "Operations",       color: "#a0522d" },
  };
  const UNKNOWN_FN = { label: "Unknown function", color: "#adb5bd" };
  const fnInfo = (f) => FUNCTIONS[f] || UNKNOWN_FN;

  let db = null;
  const state = {
    companies: [],
    companyId: null,
    company: null,        // loaded company bundle
    audit: false,         // show the generator's answer key
    expanded: new Set(),
    rules: {},
  };

  // ------------------------------------------------------------------ utils
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (x, d = 0) => (x == null ? "–" : (x * 100).toFixed(d) + "%");
  const fmt = (n) => Number(n).toLocaleString();

  function q(sql, params = []) {
    const st = db.prepare(sql);
    st.bind(params);
    const out = [];
    while (st.step()) out.push(st.getAsObject());
    st.free();
    return out;
  }

  function displayName(n) {
    if (!n) return "(no name)";
    let s = n.trim();
    if (s.includes(",")) { const [last, first] = s.split(",").map((x) => x.trim()); s = `${first} ${last}`; }
    if (s === s.toUpperCase()) s = s.toLowerCase().replace(/\b\p{L}/gu, (c) => c.toUpperCase());
    return s;
  }
  function prettyTitle(p) {
    if (p.title && p.title.trim()) return p.title.trim().replace(/\s+/g, " ");
    return "(no title)";
  }
  function confClass(c) { return c >= 0.97 ? "hi" : c >= 0.93 ? "md" : "lo"; }
  function confPill(c) { return `<span class="conf ${confClass(c)}" title="Calibrated confidence: share of edges made by this rule that were correct on calibration companies">${Math.round(c * 100)}%</span>`; }
  function dot(fn) { return `<span class="dot" style="background:${fnInfo(fn).color}" title="${esc(fnInfo(fn).label)}"></span>`; }

  // ------------------------------------------------------------------ boot
  async function loadDb() {
    const SQL = await initSqlJs({ locateFile: (f) => "vendor/" + f });
    const res = await fetch(DB_URL);
    if (!res.ok) throw new Error(`Could not fetch ${DB_URL} (${res.status})`);
    const total = Number(res.headers.get("content-length")) || 0;
    const reader = res.body.getReader();
    const chunks = [];
    let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      got += value.length;
      $("#loaderBar").style.width = total ? `${Math.min(100, (got / total) * 100)}%` : "60%";
      $("#loaderText").textContent = `${(got / 1e6).toFixed(1)} MB${total ? " of " + (total / 1e6).toFixed(1) + " MB" : ""}`;
    }
    const buf = new Uint8Array(got);
    let o = 0;
    for (const c of chunks) { buf.set(c, o); o += c.length; }
    db = new SQL.Database(buf);
  }

  async function boot() {
    initTheme();
    try {
      await loadDb();
    } catch (e) {
      $("#loaderText").innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span><br>If you opened index.html from disk, serve the folder instead: <code>python3 -m http.server</code>`;
      return;
    }
    for (const r of q("SELECT * FROM inference_rules")) state.rules[r.rule] = r;
    state.companies = q(`
      SELECT c.*,
        SUM(p.status='resolved') AS resolved, SUM(p.status='rollup') AS rollup,
        SUM(p.status='unresolved') AS unresolved, SUM(p.status='duplicate') AS dups,
        SUM(p.status!='duplicate') AS people
      FROM companies c JOIN people_inferred p ON p.company_id = c.id GROUP BY c.id`);
    for (const c of state.companies) c.coverage = c.resolved / Math.max(1, c.people - 1);
    const industries = [...new Set(state.companies.map((c) => c.industry))].sort();
    $("#industryFilter").insertAdjacentHTML("beforeend", industries.map((i) => `<option>${esc(i)}</option>`).join(""));
    $("#loader").hidden = true;
    $("#app").hidden = false;
    bindUi();
    renderCompanyList();
    route();
  }

  function initTheme() {
    let t = null;
    try { t = localStorage.getItem("orggraph-theme"); } catch (_) {}
    if (t) document.documentElement.dataset.theme = t;
    $("#themeToggle").addEventListener("click", () => {
      const cur = document.documentElement.dataset.theme ||
        (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("orggraph-theme", next); } catch (_) {}
    });
  }

  // ------------------------------------------------------------------ routing
  function route() {
    const h = location.hash.replace(/^#\/?/, "");
    const parts = h.split("/").filter(Boolean);
    let tab = "companies";
    if (parts[0] === "quality") tab = "quality";
    else if (parts[0] === "about") tab = "about";
    document.querySelectorAll(".tab").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
    $("#view-companies").hidden = tab !== "companies";
    $("#view-quality").hidden = tab !== "quality";
    $("#view-about").hidden = tab !== "about";
    if (tab !== "companies") closeDrawer(false);
    if (tab === "quality") return renderQuality();
    if (tab === "about") return renderAbout();
    if (parts[0] === "c" && parts[1]) {
      const cid = Number(parts[1]);
      if (cid !== state.companyId) openCompany(cid);
      if (parts[2] === "p" && parts[3]) openPerson(Number(parts[3]));
      else closeDrawer(false);
    } else if (!state.companyId && state.companies.length) {
      // land on a reasonably well-resolved company so the first view is meaningful
      const first = [...state.companies].sort((a, b) => b.coverage - a.coverage)[5] || state.companies[0];
      location.replace(`#/c/${first.id}`);
    }
  }

  function bindUi() {
    window.addEventListener("hashchange", route);
    $("#companyFilter").addEventListener("input", renderCompanyList);
    $("#industryFilter").addEventListener("change", renderCompanyList);
    $("#sortCompanies").addEventListener("change", renderCompanyList);
    $("#drawerClose").addEventListener("click", () => closeDrawer(true));
    $("#scrim").addEventListener("click", () => closeDrawer(true));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDrawer(true); hideSearch(); } });
    const gs = $("#globalSearch");
    let t = null;
    gs.addEventListener("input", () => { clearTimeout(t); t = setTimeout(globalSearch, 120); });
    gs.addEventListener("focus", globalSearch);
    gs.addEventListener("keydown", (e) => {
      const links = [...document.querySelectorAll("#searchResults a")];
      if (!links.length) return;
      let i = links.findIndex((a) => a.classList.contains("kb"));
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        links.forEach((a) => a.classList.remove("kb"));
        i = e.key === "ArrowDown" ? Math.min(links.length - 1, i + 1) : Math.max(0, i - 1);
        links[i].classList.add("kb");
        links[i].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter") {
        (links[i] || links[0]).click();
      }
    });
    document.addEventListener("click", (e) => { if (!e.target.closest(".search-wrap")) hideSearch(); });
  }

  // ------------------------------------------------------------------ search
  function hideSearch() { $("#searchResults").hidden = true; }
  function globalSearch() {
    const term = $("#globalSearch").value.trim();
    const box = $("#searchResults");
    if (term.length < 2) { box.hidden = true; return; }
    const like = `%${term}%`;
    const cos = q("SELECT id, name, industry FROM companies WHERE name LIKE ? ORDER BY name LIMIT 6", [like]);
    const ppl = q(`SELECT e.id, e.full_name, e.title, e.company_id, c.name AS cname
                   FROM employees e JOIN companies c ON c.id = e.company_id
                   JOIN people_inferred p ON p.employee_id = e.id
                   WHERE (e.full_name LIKE ? OR e.title LIKE ?) AND p.status != 'duplicate'
                   ORDER BY p.level DESC LIMIT 25`, [like, like]);
    let html = "";
    if (cos.length) html += `<div class="group">Companies</div>` + cos.map((c) => `<a href="#/c/${c.id}">${esc(c.name)} <span class="muted small">· ${esc(c.industry)}</span></a>`).join("");
    if (ppl.length) html += `<div class="group">People</div>` + ppl.map((p) => `<a href="#/c/${p.company_id}/p/${p.id}"><strong>${esc(displayName(p.full_name))}</strong> <span class="muted small">· ${esc(prettyTitle(p))} · ${esc(p.cname)}</span></a>`).join("");
    if (!html) html = `<div class="group">No matches</div>`;
    box.innerHTML = html;
    box.hidden = false;
    box.querySelectorAll("a").forEach((a) => a.addEventListener("click", () => { hideSearch(); $("#globalSearch").blur(); }));
  }

  // ------------------------------------------------------------------ company list
  function renderCompanyList() {
    const term = $("#companyFilter").value.trim().toLowerCase();
    const ind = $("#industryFilter").value;
    const sort = $("#sortCompanies").value;
    let list = state.companies.filter((c) => (!term || c.name.toLowerCase().includes(term)) && (!ind || c.industry === ind));
    const by = {
      name: (a, b) => a.name.localeCompare(b.name),
      coverage: (a, b) => b.coverage - a.coverage,
      coverage_asc: (a, b) => a.coverage - b.coverage,
      size: (a, b) => b.people - a.people,
    }[sort];
    list.sort(by);
    $("#companyCount").textContent = `${list.length} of ${state.companies.length} companies`;
    $("#companyList").innerHTML = list.map((c) => {
      const n = Math.max(1, c.people - 1);
      return `<li><a href="#/c/${c.id}" data-cid="${c.id}" class="${c.id === state.companyId ? "active" : ""}">
        <div class="cname">${esc(c.name)}</div>
        <div class="cmeta"><span>${esc(c.industry)} · ${c.people} people</span><span>${pct(c.resolved / n)} placed</span></div>
        <div class="mini-bar" title="Green: direct manager found · Amber: roll-up only"><span class="e" style="width:${(c.resolved / n) * 100}%"></span><span class="r" style="width:${(c.rollup / n) * 100}%"></span></div>
      </a></li>`;
    }).join("");
  }

  // ------------------------------------------------------------------ company bundle
  function loadCompany(cid) {
    const company = state.companies.find((c) => c.id === cid);
    const people = q(`
      SELECT e.id, e.full_name, e.title, e.department, e.location, e.email, e.start_date,
             p.clean_title, p.function, p.function_source, p.level, p.level_label, p.level_reason,
             p.team_qualifiers, p.city, p.duplicate_of, p.status,
             r.manager_id, r.confidence, r.rule, r.reason,
             u.ancestor_id, u.confidence AS ru_conf, u.reason AS ru_reason, u.rule AS ru_rule,
             g.true_manager_id, g.true_title
      FROM employees e
      JOIN people_inferred p ON p.employee_id = e.id
      LEFT JOIN reporting_edges r ON r.employee_id = e.id
      LEFT JOIN rollups u ON u.employee_id = e.id
      LEFT JOIN ground_truth g ON g.employee_id = e.id
      WHERE e.company_id = ?`, [cid]);
    const byId = new Map(people.map((p) => [p.id, p]));
    const reports = new Map();     // manager -> direct reports
    const rollupKids = new Map();  // ancestor -> people who roll up (direct manager unknown)
    for (const p of people) {
      p.name = displayName(p.full_name);
      p.reports = [];
      p.rollups = [];
      if (p.manager_id) (reports.get(p.manager_id) || reports.set(p.manager_id, []).get(p.manager_id)).push(p);
      if (p.ancestor_id) (rollupKids.get(p.ancestor_id) || rollupKids.set(p.ancestor_id, []).get(p.ancestor_id)).push(p);
    }
    const sorter = (a, b) => (b.level ?? -1) - (a.level ?? -1) || a.name.localeCompare(b.name);
    for (const p of people) {
      p.reports = (reports.get(p.id) || []).sort(sorter);
      p.rollups = (rollupKids.get(p.id) || []).sort(sorter);
      p.size = 0;
    }
    // subtree sizes
    const sizeOf = (p, seen = new Set()) => {
      if (seen.has(p.id)) return 0;
      seen.add(p.id);
      let s = 0;
      for (const k of p.reports) s += 1 + sizeOf(k, seen);
      s += p.rollups.length;
      p.size = s;
      return s;
    };
    const cands = new Map();
    for (const r of q("SELECT employee_id, candidate_id FROM manager_candidates WHERE company_id = ?", [cid])) {
      (cands.get(r.employee_id) || cands.set(r.employee_id, []).get(r.employee_id)).push(r.candidate_id);
    }
    const root = people.find((p) => p.status === "top");
    // roots of "floating" teams: unresolved / rollup people that have direct reports
    const floating = people.filter((p) => p.status !== "top" && p.status !== "resolved" && p.status !== "duplicate" && p.reports.length);
    for (const p of [root, ...floating]) if (p) sizeOf(p);
    const unplaced = people.filter((p) => p.status === "unresolved" && !p.reports.length);
    return { company, people, byId, root, floating, unplaced, cands };
  }

  function openCompany(cid) {
    state.companyId = cid;
    state.company = loadCompany(cid);
    state.expanded = new Set();
    const { root } = state.company;
    if (root) {
      state.expanded.add(root.id);
    }
    document.querySelectorAll("#companyList a").forEach((a) => a.classList.toggle("active", Number(a.dataset.cid) === cid));
    const act = $(`#companyList a[data-cid="${cid}"]`);
    if (act) act.scrollIntoView({ block: "nearest" });
    renderCompany();
  }

  // ------------------------------------------------------------------ company view
  function functionLeaders(bundle) {
    const out = {};
    for (const p of bundle.people) {
      if (p.status === "duplicate" || !p.function || p.function === "executive" || p.level == null || p.level < 4) continue;
      const cur = out[p.function];
      if (!cur || p.level > cur[0].level) out[p.function] = [p];
      else if (p.level === cur[0].level) cur.push(p);
    }
    return out;
  }

  function renderCompany() {
    const b = state.company;
    const c = b.company;
    const n = Math.max(1, c.people - 1);
    const leaders = functionLeaders(b);
    const fnOrder = Object.keys(FUNCTIONS).filter((f) => leaders[f]);
    const avgConf = q("SELECT AVG(confidence) AS a FROM reporting_edges WHERE company_id = ?", [c.id])[0].a;

    const whoHtml = fnOrder.map((f) => {
      const ls = leaders[f];
      const p = ls[0];
      const more = ls.length > 1 ? ` <span class="tag" title="Several people share the top title">+${ls.length - 1}</span>` : "";
      return `<button class="who" data-pid="${p.id}">
        <div class="fn">${dot(f)} Who runs ${esc(fnInfo(f).label)}?</div>
        <div class="nm">${esc(p.name)}${more}</div>
        <div class="tt">${esc(prettyTitle(p))}</div></button>`;
    }).join("");

    const legend = `<div class="legend">
      <span><span class="conf hi">97%+</span> high</span><span><span class="conf md">93–97%</span> good</span>
      <span><span class="conf lo">&lt;93%</span> fair</span><span><span class="conf ru">roll-up</span> under this leader, direct manager unclear</span>
    </div>`;

    $("#companyContent").innerHTML = `
      <div class="co-head">
        <div>
          <h1>${esc(c.name)}</h1>
          <div class="chips">
            <span class="chip">${esc(c.industry)}</span><span class="chip">HQ ${esc(c.hq_city)}, ${esc(c.country)}</span>
            <span class="chip">Founded ${c.founded_year}</span><span class="chip">${esc(c.domain)}</span>
          </div>
        </div>
        <label class="chip" style="cursor:pointer" title="Compare against the synthetic generator's hidden answer key">
          <input type="checkbox" id="auditToggle" ${state.audit ? "checked" : ""}> Show answer key
        </label>
      </div>
      <div class="stats">
        <div class="stat"><div class="v">${c.people}</div><div class="l">people (${c.dups} duplicate rows merged)</div></div>
        <div class="stat"><div class="v">${pct(c.resolved / n)}</div><div class="l">have a direct manager edge</div></div>
        <div class="stat"><div class="v">${pct((c.resolved + c.rollup) / n)}</div><div class="l">placed (edge or roll-up)</div></div>
        <div class="stat"><div class="v">${pct(avgConf)}</div><div class="l">average edge confidence</div></div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Who runs what</h2><span class="muted small">Most senior person found in each function</span></div>
        <div class="who-grid">${whoHtml || '<span class="muted">No function leaders found</span>'}</div>
      </div>
      <div class="card">
        <div class="card-head">
          <h2>Org chart</h2>
          <div class="actions">
            <button class="btn" id="expandAll">Expand all</button>
            <button class="btn" id="expandLeaders">Leaders only</button>
            <button class="btn" id="collapseAll">Collapse</button>
          </div>
        </div>
        ${legend}
        <div style="margin-top:10px">${b.root ? `<ul class="tree" id="mainTree"></ul>` : `<div class="callout warn">No single CEO could be identified, so the chart has no anchor. The teams we could build are shown below.</div>`}</div>
      </div>
      ${b.floating.length ? `<div class="card">
        <div class="card-head"><h2>Teams we couldn't attach</h2><span class="muted small">Managers found with their teams, but the manager's own boss is unclear</span></div>
        <ul class="tree" id="floatTree"></ul></div>` : ""}
      <div class="card">
        <div class="card-head"><h2>Not placed (${b.unplaced.length})</h2><span class="muted small">Left empty on purpose: a wrong edge is worse than no edge</span></div>
        <div id="unplaced"></div>
      </div>`;

    $("#auditToggle").addEventListener("change", (e) => { state.audit = e.target.checked; renderTrees(); });
    $("#expandAll").addEventListener("click", () => { b.people.forEach((p) => state.expanded.add(p.id)); renderTrees(); });
    $("#collapseAll").addEventListener("click", () => { state.expanded = new Set(b.root ? [b.root.id] : []); renderTrees(); });
    $("#expandLeaders").addEventListener("click", () => {
      state.expanded = new Set(b.people.filter((p) => p.level >= 5 || p.status === "top").map((p) => p.id));
      renderTrees();
    });
    document.querySelectorAll(".who").forEach((el) => el.addEventListener("click", () => goPerson(Number(el.dataset.pid))));
    renderTrees();
    renderUnplaced();
  }

  function nodeHtml(p, kind) {
    const kids = p.reports.length + p.rollups.length;
    const open = state.expanded.has(p.id);
    let edge = "";
    if (kind === "edge") edge = confPill(p.confidence);
    else if (kind === "rollup") edge = `<span class="conf ru" title="${esc(p.ru_reason || "")}">roll-up</span>`;
    let audit = "";
    if (state.audit && p.status !== "top") {
      if (kind === "edge") audit = p.true_manager_id === p.manager_id ? `<span class="tag good">✓ correct</span>` : `<span class="tag bad">✗ wrong</span>`;
      else if (kind === "rollup") audit = isTrueAncestor(p, p.ancestor_id) ? `<span class="tag good">✓ true ancestor</span>` : `<span class="tag bad">✗ wrong</span>`;
    }
    let li = `<li class="${kind === "rollup" ? "rollup" : ""}">
      <div class="node" data-pid="${p.id}">
        <button class="twisty ${kids ? "" : "leaf"}" data-toggle="${p.id}" aria-label="Toggle">${open ? "▼" : "▶"}</button>
        ${dot(p.function)}
        <span class="nm">${esc(p.name)}</span>
        <span class="tt">${esc(prettyTitle(p))}</span>
        <span class="spacer"></span>
        ${kids ? `<span class="cnt">${p.reports.length ? p.reports.length + " direct" : ""}${p.rollups.length ? (p.reports.length ? " · " : "") + p.rollups.length + " roll-up" : ""}</span>` : ""}
        ${audit}${edge}
      </div>`;
    if (kids && open) {
      li += `<ul>${p.reports.map((k) => nodeHtml(k, "edge")).join("")}${p.rollups.map((k) => nodeHtml(k, "rollup")).join("")}</ul>`;
    }
    return li + "</li>";
  }

  function isTrueAncestor(p, ancId) {
    const byId = state.company.byId;
    let m = p.true_manager_id, guard = 0;
    while (m && guard++ < 50) { if (m === ancId) return true; m = byId.get(m)?.true_manager_id; }
    return false;
  }

  function renderTrees() {
    const b = state.company;
    const main = $("#mainTree");
    if (main && b.root) main.innerHTML = nodeHtml(b.root, "root");
    const ft = $("#floatTree");
    if (ft) ft.innerHTML = b.floating.map((p) => nodeHtml(p, "float")).join("");
    for (const el of document.querySelectorAll("#mainTree, #floatTree")) {
      el.onclick = (e) => {
        const t = e.target.closest("[data-toggle]");
        if (t) {
          const id = Number(t.dataset.toggle);
          state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
          renderTrees();
          return;
        }
        const n = e.target.closest(".node");
        if (n) goPerson(Number(n.dataset.pid));
      };
    }
  }

  function renderUnplaced() {
    const b = state.company;
    const groups = {};
    for (const p of b.unplaced) (groups[p.function || "_"] ||= []).push(p);
    const keys = Object.keys(groups).sort((a, b2) => groups[b2].length - groups[a].length);
    $("#unplaced").innerHTML = keys.length ? keys.map((f) => `
      <div class="unplaced-group">
        <h3>${dot(f === "_" ? null : f)} ${esc(f === "_" ? "Unknown function" : fnInfo(f).label)} <span class="muted small">(${groups[f].length})</span></h3>
        <div class="plist">${groups[f].map((p) => {
          const cands = b.cands.get(p.id) || [];
          const why = p.level == null ? "Title too vague to place" :
            cands.length > 1 ? `${cands.length} possible managers` :
            cands.length === 1 ? "Only weak evidence for one manager" : "No manager-level person found";
          return `<div class="pitem" data-pid="${p.id}"><div class="nm">${esc(p.name)}</div><div class="tt">${esc(prettyTitle(p))}</div><div class="why">${why}</div></div>`;
        }).join("")}</div>
      </div>`).join("") : `<span class="muted">Everyone is placed.</span>`;
    $("#unplaced").onclick = (e) => { const it = e.target.closest(".pitem"); if (it) goPerson(Number(it.dataset.pid)); };
  }

  // ------------------------------------------------------------------ person drawer
  function goPerson(pid) { location.hash = `#/c/${state.companyId}/p/${pid}`; }

  function personCard(p, extra = "") {
    if (!p) return "";
    return `<div class="person-card" data-pid="${p.id}">${dot(p.function)}
      <div class="body"><div class="nm">${esc(p.name)}</div><div class="tt">${esc(prettyTitle(p))}</div></div>${extra}</div>`;
  }

  function chainOf(p) {
    const byId = state.company.byId;
    const chain = [];
    let cur = p, guard = 0;
    while (cur && guard++ < 30) {
      if (cur.manager_id) { cur = byId.get(cur.manager_id); chain.push({ p: cur, kind: "edge" }); }
      else if (cur.ancestor_id) { cur = byId.get(cur.ancestor_id); chain.push({ p: cur, kind: "rollup" }); }
      else break;
    }
    return chain;
  }

  function economicBuyer(p, chain) {
    if (p.level >= 7) return { who: p, note: "This person is VP-level or above and is likely the budget holder." };
    const hit = chain.find((s) => s.p.level >= 7);
    if (hit) return { who: hit.p, note: `First VP or C-level leader above ${esc(p.name)} in the inferred chain.` };
    const top = chain.length ? chain[chain.length - 1].p : null;
    if (top && top.status === "top") {
      const lastNonCeo = [...chain].reverse().find((s) => s.p.status !== "top");
      if (lastNonCeo) return { who: lastNonCeo.p, note: "No VP in this chain. The function head is the most senior budget owner below the CEO." };
      return { who: top, note: "Reports straight to the CEO." };
    }
    const leaders = functionLeaders(state.company)[p.function];
    if (leaders && leaders[0].id !== p.id) {
      return { who: leaders[0], weak: true, note: "The reporting chain breaks before reaching a VP. This is the most senior person in the same function, not a confirmed line." };
    }
    return null;
  }

  function openPerson(pid) {
    const b = state.company;
    if (!b) return;
    const p = b.byId.get(pid);
    if (!p) return;
    const byId = b.byId;
    if (p.status === "duplicate") { const orig = byId.get(p.duplicate_of); if (orig) return goPerson(orig.id); }
    const chain = chainOf(p);
    const rule = p.rule ? state.rules[p.rule] : null;

    let mgrHtml;
    if (p.status === "top") {
      mgrHtml = `<div class="callout info">Top of the company. No manager.</div>`;
    } else if (p.manager_id) {
      const m = byId.get(p.manager_id);
      mgrHtml = personCard(m, confPill(p.confidence)) +
        `<div class="reason">${esc(p.reason)}.${rule ? ` Rule <span class="mono">${esc(p.rule)}</span> was right ${pct(rule.test_precision, 1)} of the time on ${fmt(rule.test_n)} held-out cases.` : ""}</div>`;
    } else if (p.ancestor_id) {
      const a = byId.get(p.ancestor_id);
      const cands = (b.cands.get(p.id) || []).map((id) => byId.get(id)).filter(Boolean);
      mgrHtml = `<div class="callout warn">Direct manager unclear. ${esc(p.ru_reason)}.</div>
        <div class="section"><h4>Rolls up to (${pct(p.ru_conf)} confidence)</h4>${personCard(a)}</div>
        ${cands.length ? `<div class="section"><h4>Possible direct managers (not asserted)</h4>${cands.map((c) => personCard(c)).join("")}</div>` : ""}`;
    } else {
      const cands = (b.cands.get(p.id) || []).map((id) => byId.get(id)).filter(Boolean);
      const why = p.level == null ? "The title is too vague to place this person." :
        cands.length > 1 ? `There are ${cands.length} equally plausible managers, so none is asserted.` :
        cands.length === 1 ? "There's only weak evidence for one manager, below the precision bar, so the edge is left empty." :
        "No manager-level person in this function was found.";
      mgrHtml = `<div class="callout warn">No manager assigned. ${why}</div>
        ${cands.length ? `<div class="section"><h4>Candidates (not asserted)</h4>${cands.map((c) => personCard(c)).join("")}</div>` : ""}`;
    }

    const chainHtml = chain.length ? `<div class="chain">${[{ p, kind: "self" }, ...chain].map((s, i, arr) => `
      <div class="step"><div class="rail"><i style="background:${fnInfo(s.p.function).color}"></i>${i < arr.length - 1 ? "<b></b>" : ""}</div>
        ${personCard(s.p, s.kind === "rollup" ? '<span class="conf ru">roll-up</span>' : (i < arr.length - 1 && arr[i].p.confidence && arr[i].kind !== "rollup" ? confPill(arr[i].p.confidence) : ""))}</div>`).join("")}</div>` :
      `<span class="muted">No chain available.</span>`;

    const eb = p.status !== "top" ? economicBuyer(p, chain) : null;
    const ebHtml = eb ? `<div class="callout ${eb.weak ? "warn" : "good"}" style="margin-bottom:8px">${eb.note}</div>${personCard(eb.who)}` : "";

    const reports = p.reports;
    const repHtml = reports.length ? reports.map((r) => personCard(r, confPill(r.confidence))).join("") : `<span class="muted">None inferred.</span>`;
    const rollHtml = p.rollups.length ? `<div class="section"><h4>Also rolls up here (${p.rollups.length}, direct manager unclear)</h4>${p.rollups.slice(0, 30).map((r) => personCard(r)).join("")}</div>` : "";

    let auditHtml = "";
    if (state.audit) {
      const tm = byId.get(p.true_manager_id);
      const ok = p.manager_id ? p.manager_id === p.true_manager_id : null;
      auditHtml = `<div class="section"><h4>Answer key (synthetic data only)</h4>
        <div class="callout ${ok === null ? "info" : ok ? "good" : "bad"}">${ok === null ? "No edge asserted." : ok ? "The inferred manager is correct." : "The inferred manager is wrong."}
        True title: <strong>${esc(p.true_title)}</strong></div>${tm ? personCard(tm) : '<span class="muted small">No true manager (top of company).</span>'}</div>`;
    }

    $("#drawerBody").innerHTML = `
      <div class="muted small">${esc(b.company.name)}</div>
      <h2>${esc(p.name)}</h2>
      <div class="sub">${esc(prettyTitle(p))}</div>
      <div class="chips" style="margin-top:8px">
        <span class="chip">${dot(p.function)} ${esc(fnInfo(p.function).label)}</span>
        <span class="chip">${esc(p.level_label || "Unknown level")}</span>
        ${p.team_qualifiers ? `<span class="chip">Team: ${esc(p.team_qualifiers)}</span>` : ""}
      </div>
      <div class="section"><h4>Manager</h4>${mgrHtml}</div>
      ${eb ? `<div class="section"><h4>Economic buyer above this person</h4>${ebHtml}</div>` : ""}
      <div class="section"><h4>Chain to the top</h4>${chainHtml}</div>
      <div class="section"><h4>Direct reports (${reports.length})</h4>${repHtml}</div>
      ${rollHtml}
      ${auditHtml}
      <div class="section"><h4>How the title was read</h4>
        <dl class="kv">
          <dt>Raw title</dt><dd>${esc(p.title ?? "(missing)")}</dd>
          <dt>Normalised</dt><dd class="mono">${esc(p.clean_title || "–")}</dd>
          <dt>Level</dt><dd>${esc(p.level_label)} <span class="muted small">(${esc(p.level_reason)})</span></dd>
          <dt>Function</dt><dd>${esc(fnInfo(p.function).label)} <span class="muted small">(from ${esc(p.function_source)})</span></dd>
          <dt>Department</dt><dd>${esc(p.department ?? "(missing)")}</dd>
          <dt>Location</dt><dd>${esc(p.location ?? "(missing)")}</dd>
          <dt>Email</dt><dd>${esc(p.email ?? "(missing)")}</dd>
          <dt>Start date</dt><dd>${esc(p.start_date ?? "(missing)")}</dd>
        </dl>
      </div>`;
    $("#drawerBody").onclick = (e) => { const c = e.target.closest(".person-card"); if (c) goPerson(Number(c.dataset.pid)); };
    $("#drawer").classList.add("open");
    $("#drawer").setAttribute("aria-hidden", "false");
    $("#scrim").hidden = false;
    $("#drawer").scrollTop = 0;

    // reveal the person in the tree
    let cur = p, guard = 0;
    while (cur && guard++ < 30) {
      const up = cur.manager_id || cur.ancestor_id;
      if (!up) break;
      state.expanded.add(up);
      cur = byId.get(up);
    }
    renderTrees();
    document.querySelectorAll(".node.hl").forEach((n) => n.classList.remove("hl"));
    const node = document.querySelector(`.node[data-pid="${pid}"]`);
    if (node) { node.classList.add("hl"); node.scrollIntoView({ block: "center" }); }
  }

  function closeDrawer(updateHash) {
    $("#drawer").classList.remove("open");
    $("#drawer").setAttribute("aria-hidden", "true");
    $("#scrim").hidden = true;
    if (updateHash && state.companyId && /\/p\//.test(location.hash)) location.hash = `#/c/${state.companyId}`;
  }

  // ------------------------------------------------------------------ quality page
  function renderQuality() {
    const m = Object.fromEntries(q("SELECT key, value FROM metrics").map((r) => [r.key, r.value]));
    const status = Object.fromEntries(q("SELECT status, COUNT(*) AS n FROM people_inferred GROUP BY status").map((r) => [r.status, r.n]));
    const rules = q("SELECT * FROM inference_rules ORDER BY accepted DESC, train_n DESC");
    const wrong = q(`
      SELECT e.id, e.company_id, e.full_name, e.title, m.full_name AS mname, m.title AS mtitle,
             t.full_name AS tname, t.title AS ttitle, r.confidence, r.rule
      FROM reporting_edges r JOIN employees e ON e.id = r.employee_id
      JOIN ground_truth g ON g.employee_id = e.id
      JOIN employees m ON m.id = r.manager_id LEFT JOIN employees t ON t.id = g.true_manager_id
      WHERE g.true_manager_id IS NOT r.manager_id AND e.company_id % 2 = 1
      ORDER BY r.confidence DESC LIMIT 25`);
    const kpi = (v, l) => `<div class="stat"><div class="v">${v}</div><div class="l">${l}</div></div>`;
    const bar = (x) => x == null ? "" : `<span class="pbar"><i style="width:${x * 100}%;background:${x >= 0.95 ? "var(--good)" : x >= 0.9 ? "var(--warn)" : "var(--bad)"}"></i></span>`;
    $("#qualityContent").innerHTML = `
      <h1 style="font-size:24px">Model quality</h1>
      <p class="muted" style="max-width:760px">The data is synthetic, so the generator's hidden answer key lets us score every edge. Each inference rule is calibrated on the even-numbered companies, and its precision there becomes the confidence of every edge it makes. A rule only writes edges if it clears <strong>${pct(m.min_precision)}</strong> precision. The numbers below come from the odd-numbered (held-out) companies.</p>
      <div class="kpis">
        ${kpi(pct(m.test_precision, 1), "edge precision (held-out)")}
        ${kpi(pct(m.test_coverage, 1), "people with a direct manager edge")}
        ${kpi(pct(m.test_placed, 1), "placed by an edge or a roll-up")}
        ${kpi(pct(m.test_rollup_precision, 1), "roll-up precision")}
        ${kpi(fmt(m.test_wrong_edges), `wrong edges out of ${fmt(m.test_edges)}`)}
        ${kpi(pct(m.dup_recall, 0), "duplicate rows caught")}
      </div>
      <div class="card">
        <div class="card-head"><h2>Where everyone ended up</h2><span class="muted small">All ${fmt(m.employees)} rows across ${fmt(m.companies)} companies</span></div>
        <div class="kpis" style="margin:0">
          ${kpi(fmt(status.resolved || 0), "direct manager edge")}${kpi(fmt(status.rollup || 0), "roll-up only")}
          ${kpi(fmt(status.unresolved || 0), "left unresolved")}${kpi(fmt(status.top || 0), "CEOs (top)")}${kpi(fmt(status.duplicate || 0), "duplicates merged")}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Decision rules</h2><span class="muted small">Rejected rules still show up as candidates in the UI, never as edges</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Rule</th><th>Used</th><th class="num">Calibration n</th><th class="num">Calibration precision</th><th class="num">Held-out n</th><th class="num">Held-out precision</th></tr></thead>
          <tbody>${rules.map((r) => `<tr><td class="mono">${esc(r.rule)}</td>
            <td>${r.accepted ? `<span class="tag good">${r.kind === "rollup" ? "roll-up" : "edge"}</span>` : '<span class="tag bad">rejected</span>'}</td>
            <td class="num">${fmt(r.train_n)}</td><td class="num">${pct(r.train_precision, 1)}${bar(r.train_precision)}</td>
            <td class="num">${fmt(r.test_n)}</td><td class="num">${pct(r.test_precision, 1)}${bar(r.test_precision)}</td></tr>`).join("")}</tbody>
        </table></div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Highest-confidence mistakes (held-out)</h2><span class="muted small">Mostly cases where the real manager has a vague or missing title</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Person</th><th>Inferred manager</th><th>True manager</th><th class="num">Conf.</th></tr></thead>
          <tbody>${wrong.map((w) => `<tr>
            <td><a href="#/c/${w.company_id}/p/${w.id}">${esc(displayName(w.full_name))}</a><div class="muted small">${esc(w.title ?? "(no title)")}</div></td>
            <td>${esc(displayName(w.mname))}<div class="muted small">${esc(w.mtitle ?? "(no title)")}</div></td>
            <td>${esc(displayName(w.tname))}<div class="muted small">${esc(w.ttitle ?? "(no title)")}</div></td>
            <td class="num">${pct(w.confidence)}</td></tr>`).join("")}</tbody>
        </table></div>
      </div>`;
  }

  // ------------------------------------------------------------------ about page
  function renderAbout() {
    $("#aboutContent").innerHTML = `
      <h1>How OrgGraph works</h1>
      <p class="muted">A static site. The browser downloads one SQLite file and queries it with sql.js (SQLite compiled to WebAssembly). There is no server.</p>
      <h2>1. The data (generate_data.py)</h2>
      <p>There is no real dataset, so a Python script (using Faker) creates 520 companies of 100 to 150 people across 12 industries. It first builds a true reporting tree for each company: CEO, C-suite, VPs, heads, directors, managers and individual contributors across 14 functions. Then it throws the tree away and keeps only a messy flat list, the way a data vendor would hand it over:</p>
      <ul>
        <li>Abbreviations and reordering: <code>Sr. SWE | Payments</code>, <code>Mgr, Sales, EMEA</code>, <code>Vice Pres., Marketing</code></li>
        <li>Typos, ALL CAPS, emojis, <code>| We're hiring!</code>, <code>Interim</code> or <code>Acting</code> prefixes, and some titles in other languages (<code>Vertriebsleiter</code>, <code>Chef de produit</code>)</li>
        <li>Missing or useless titles (<code>Team Member</code>, <code>N/A</code>), missing departments and locations, and inconsistent department names (<code>GTM</code>, <code>R&amp;D</code>, <code>People Ops</code>)</li>
        <li>Duplicate rows for the same person, and names written as <code>LAST, First</code></li>
        <li>Misleading titles: Product Manager, Account Manager and Customer Success Manager are not people managers</li>
      </ul>
      <h2>2. The inference (infer.py)</h2>
      <ol>
        <li><strong>Normalise</strong> each title: strip noise, expand abbreviations, fix typos by fuzzy matching, and translate common foreign titles.</li>
        <li><strong>Parse</strong> it into a seniority level (CEO → C-suite → VP → Head → Director → Manager → IC), a function (with the department field as a fallback), and team qualifiers such as <code>payments</code> or <code>emea</code>.</li>
        <li><strong>Resolve</strong> managers top-down. The candidates for each person are manager-tier people in the same function at the nearest level above. Ties are broken by a shared team name, then by city. The head of a function attaches to the parent function's leader (Data and Security go under Engineering, Design under Product, Support under Customer Success) or to the CEO.</li>
        <li><strong>Calibrate</strong>: every decision is tagged with the rule that produced it. A rule's precision on the calibration companies becomes the confidence of its edges. Rules below the bar write no edges at all.</li>
        <li><strong>Don't guess</strong>: if several managers are equally plausible but all report to the same leader, the person gets a <em>roll-up</em> to that leader instead of a direct edge, and the plausible managers are listed as candidates.</li>
      </ol>
      <h2>3. Regenerating</h2>
<pre>pip install -r requirements.txt
python generate_data.py      # writes data/orgchart.db
python infer.py              # adds the inferred tables to it
python -m http.server 8000   # open http://localhost:8000</pre>
      <h2>Tables in orgchart.db</h2>
      <p><code>companies</code> and <code>employees</code> hold the raw input. <code>ground_truth</code> is the generator's answer key, used only for scoring. <code>people_inferred</code>, <code>reporting_edges</code>, <code>rollups</code>, <code>manager_candidates</code>, <code>inference_rules</code> and <code>metrics</code> are the inference output.</p>`;
  }

  boot();
})();
