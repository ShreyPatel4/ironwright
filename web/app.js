/* ironwright demo: renders one pack snapshot at a time. Data comes from window.IRONWRIGHT_DATA
   (inlined build) or data/<pack>.json (static hosting). No framework, no build step. */
(function () {
  const PACKS = [
    { id: "retail", short: "Retail" },
    { id: "supply_chain", short: "Supply chain" },
    { id: "banking_aml", short: "Banking AML" },
    { id: "insurance_claims", short: "Insurance" },
  ];
  const cache = {};
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n) => (typeof n === "number" ? n.toLocaleString("en-US", { maximumFractionDigits: 2 }) : esc(n));
  const pretty = (s) => esc(String(s || "").replace(/_/g, " "));
  const layerOf = (t) => t.split(".")[0];
  const LAYER_C = { bronze: "var(--l-bronze)", silver: "var(--l-silver)", gold: "var(--l-gold)" };

  async function load(id) {
    if (cache[id]) return cache[id];
    if (window.IRONWRIGHT_DATA && window.IRONWRIGHT_DATA[id]) return (cache[id] = window.IRONWRIGHT_DATA[id]);
    const r = await fetch(`data/${id}.json`);
    if (!r.ok) throw new Error(`no data for ${id}`);
    return (cache[id] = await r.json());
  }

  function statusChip(st) {
    const map = { applied: "pass", applied_pending_sync: "pass", needs_approval: "warn", denied: "fail", rejected: "fail",
      rejected_by_reviewer: "fail", no_action: "plain", replayed: "info" };
    return `<span class="chip ${map[st] || "plain"}">${pretty(st)}</span>`;
  }

  // ---------------- sections ----------------
  function renderStrip(d) {
    const w = d.migrate?.writes || [];
    const by = (l) => w.filter((x) => layerOf(x.table) === l);
    const rows = (l) => by(l).reduce((a, b) => a + b.rows, 0);
    const objs = Object.values(d.ontology?.objects_by_type || {}).reduce((a, b) => a + b, 0);
    const runs = Object.values(d.agents || {}).flatMap((u) => u.runs);
    const ev = d.evals?.layers || [];
    const cells = [
      ["#legacy", "var(--l-raw)", "Legacy", d.legacy.length, "systems and feeds"],
      ["#lake", "var(--l-bronze)", "Bronze", fmt(rows("bronze")), `rows, ${by("bronze").length} tables`],
      ["#lake", "var(--l-silver)", "Silver", fmt(rows("silver")), `rows, ${by("silver").length} tables`],
      ["#lake", "var(--l-gold)", "Gold", fmt(rows("gold")), `rows, ${by("gold").length} tables`],
      ["#ontology", "var(--l-ont)", "Ontology", fmt(objs), "objects"],
      ["#agents", "var(--l-app)", "Agents", runs.length, "decisions"],
      ["#evals", d.evals?.passed ? "var(--pass)" : "var(--fail)", "Evals", `${ev.filter((l) => l.passed).length}/${ev.length}`, "layers pass"],
    ];
    return `<nav class="strip" aria-label="the stack">${cells.map(([h, c, k, v, s]) =>
      `<a href="${h}" style="--c:${c}"><span class="k">${k}</span><span class="v">${v}</span><span class="s">${s}</span></a>`).join("")}</nav>`;
  }

  function renderStory(d) {
    return `<section id="story"><h2>${esc(d.title)} <span class="chip plain">${esc(d.industry)}</span></h2>
      <p class="lede">${esc(d.tagline)}</p>
      <p class="story">${esc(d.story)}</p>
      <div class="decision">The first decision the agent takes on: ${esc(d.decision)}</div>
      <h3>What the generator planted in the legacy systems (the evals check every one)</h3>
      <ul class="planted">${d.planted.map((p) => `<li><span class="kind">${esc(p.kind)}</span>${esc(p.what)}</li>`).join("")}</ul></section>`;
  }

  function renderLegacy(d) {
    return `<section id="legacy" style="--c:var(--l-raw)"><h2><span class="layer">raw sources</span>The legacy systems, as received</h2>
      <p class="lede">Fixed-width mainframe files, old operational databases, CSV drops and event streams. Nothing is trusted on the way in: bronze lands every field as a string.</p>
      <div class="grid g2">${d.legacy.map((s) => `<div class="card"><h4>${esc(s.label)}</h4>
        <div class="meta">${esc(s.system)} · ${esc(s.kind)} · ${esc(s.name)}</div>
        <pre class="raw">${esc(s.sample || "(binary database)")}</pre></div>`).join("")}</div></section>`;
  }

  function renderLake(d) {
    const w = d.migrate?.writes || [];
    const flagged = w.flatMap((x) => x.checks.filter((c) => !c.passed));
    const sevChip = (s) => (s === "block" ? "fail" : s === "quarantine" ? "q" : "warn");
    return `<section id="lake" style="--c:var(--l-bronze)"><h2><span class="layer">logic layer</span>Lakehouse with Write Audit Publish</h2>
      <p class="lede">Every write lands on an Iceberg branch, quality checks run on that branch, and main is fast-forwarded only if no blocking check fails. Bad rows can be quarantined instead of blocking the table. Run <span class="mono">${esc(d.migrate?.run_id)}</span>.</p>
      ${flagged.length ? `<h3>What the gates caught</h3><div class="tbl"><table><thead><tr><th>Table</th><th>Check</th><th>Kind</th><th>Outcome</th><th class="num">Rows</th><th>Owner</th><th>Detail</th></tr></thead><tbody>
        ${flagged.map((c) => `<tr><td class="mono">${esc(c.table)}</td><td>${pretty(c.check)}</td><td>${esc(c.kind)}</td>
        <td><span class="chip ${sevChip(c.severity)}">${esc(c.severity === "quarantine" ? "quarantined" : c.severity)}</span></td>
        <td class="num">${fmt(c.failing_rows)}</td><td class="mono">${esc(c.owner)}</td><td>${esc(c.detail)}</td></tr>`).join("")}</tbody></table></div>` : ""}
      <h3>Every table, every gate</h3>
      <div class="tbl"><table><thead><tr><th>Table</th><th class="num">Rows</th><th>Checks</th><th>Published</th><th>Snapshot</th></tr></thead><tbody>
      ${w.map((x) => `<tr><td><span class="layerdot" style="--c:${LAYER_C[layerOf(x.table)]}"></span><span class="mono">${esc(x.table)}</span></td>
        <td class="num">${fmt(x.rows)}</td>
        <td>${x.checks.length ? `${x.checks.filter((c) => c.passed).length}/${x.checks.length} pass` : `<span class="chip plain">none</span>`}</td>
        <td>${x.published ? `<span class="chip pass">published</span>` : `<span class="chip fail">blocked</span>`}</td>
        <td class="mono">${esc((x.snapshot_id || "").slice(0, 10))}</td></tr>`).join("")}</tbody></table></div></section>`;
  }

  function renderRecon(d) {
    const r = d.reconcile || { rules: [] };
    return `<section id="reconcile" style="--c:var(--l-silver)"><h2><span class="layer">migration</span>Reconciliation with legacy</h2>
      <p class="lede">The bar for cutover: the new platform matches what the legacy system reported, and every difference is explained. Cutover is <strong>${r.cutover_ready ? "ready" : "blocked"}</strong>.</p>
      <div class="grid g2">${r.rules.map((x) => `<div class="card"><div class="eval-head"><h4>${esc(x.name)}</h4>${x.passed ? `<span class="chip pass">pass</span>` : `<span class="chip fail">fail</span>`}</div>
        <div class="meta">key ${esc(x.key.join(", "))} · ${x.counts.match} match · ${x.counts.explained} explained · ${x.counts.unexplained} unexplained</div>
        ${x.differences.filter((y) => y.status === "explained").slice(0, 3).map((y) => `<p style="margin:10px 0 0;font-size:13.5px">
          <span class="chip info">${esc(Object.values(y.key).join(" "))}</span>
          legacy ${x.measures.map((m) => `${esc(m)} ${fmt(y["legacy_" + m])}`).join(", ")} vs new ${x.measures.map((m) => `${fmt(y["new_" + m])}`).join(", ")}.
          ${esc(y.reason || "")}</p>`).join("")}
      </div>`).join("")}</div></section>`;
  }

  function renderGraph(d) {
    const g = d.ontology?.graph;
    if (!g || !g.nodes.length) return "";
    const types = [...new Set(g.nodes.map((n) => n.type))];
    const cols = types.map((t) => g.nodes.filter((n) => n.type === t).slice(0, 9));
    const W = Math.max(720, types.length * 210), colW = W / types.length, rowH = 34;
    const H = Math.max(...cols.map((c) => c.length)) * rowH + 70;
    const pos = {};
    cols.forEach((c, i) => c.forEach((n, j) => (pos[n.id] = { x: colW * i + colW / 2, y: 56 + j * rowH + (H - 56 - c.length * rowH) / 2 })));
    const edges = g.edges.filter((e) => pos[e.from] && pos[e.to]).slice(0, 60);
    const nodeW = Math.min(180, colW - 24);
    return `<div class="card graph"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="ontology neighborhood">
      ${types.map((t, i) => `<text class="tlabel" x="${colW * i + colW / 2}" y="24" text-anchor="middle">${esc(t)}</text>`).join("")}
      ${edges.map((e) => { const a = pos[e.from], b = pos[e.to]; const mx = (a.x + b.x) / 2;
        return `<path class="edge" d="M${a.x},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x},${b.y}"><title>${esc(e.link)}</title></path>`; }).join("")}
      ${Object.entries(pos).map(([id, p]) => { const n = g.nodes.find((x) => x.id === id);
        return `<g><rect x="${p.x - nodeW / 2}" y="${p.y - 12}" width="${nodeW}" height="24" rx="6" fill="var(--surface)" stroke="var(--l-ont)" stroke-width="1.2"></rect>
        <text x="${p.x}" y="${p.y + 4}" text-anchor="middle">${esc(String(n.title).slice(0, 24))}</text><title>${esc(n.type)} ${esc(id)}</title></g>`; }).join("")}
    </svg></div>`;
  }

  function renderOntology(d) {
    const o = d.ontology, s = o.schema, rep = o.report || { object_types: {}, links: {} };
    return `<section id="ontology" style="--c:var(--l-ont)"><h2><span class="layer">ontology</span>The business, not the tables</h2>
      <p class="lede">Object types with one owner each, identity resolved across systems through a crosswalk, links, governed functions to read and actions to write. Defined as code in <span class="mono">ontology.yaml</span>.</p>
      <div class="tbl"><table><thead><tr><th>Object type</th><th>Owner</th><th>From systems</th><th class="num">Source records</th><th class="num">Objects</th><th class="num">Merged</th><th>Description</th></tr></thead><tbody>
      ${Object.entries(s.object_types).map(([t, v]) => { const r = rep.object_types[t] || {};
        return `<tr><td><strong>${esc(t)}</strong></td><td class="mono">${esc(v.owner)}</td><td>${v.sources.map((x) => `<span class="chip plain">${esc(x)}</span>`).join(" ")}</td>
        <td class="num">${fmt(r.candidates)}</td><td class="num">${fmt(r.objects)}</td><td class="num">${fmt(r.merged_clusters)}</td><td>${esc(v.description)}</td></tr>`; }).join("")}
      </tbody></table></div>
      <h3>Identity resolution: one real thing, many source keys</h3>
      <div class="grid g3">${o.merges.slice(0, 6).map((m) => `<div class="card"><h4>${esc(m.title)}</h4>
        <div class="meta">${esc(m.type)} · ${esc(m.id)} · ${esc(m.method)} (${fmt(m.confidence)})</div>
        <ul style="margin:8px 0 0;padding-left:18px;font:12.5px var(--mono)">${m.members.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
        ${Object.keys(m.provenance || {}).length ? `<div class="meta" style="margin-top:6px">fields from: ${Object.entries(m.provenance).slice(0, 4).map(([k, v]) => `${esc(k)}←${esc(v)}`).join(", ")}</div>` : ""}</div>`).join("")}</div>
      <h3>A slice of the graph around the agent's first cases</h3>${renderGraph(d)}
      <div class="grid g2" style="margin-top:12px">
        <div class="card"><h4>Functions (governed reads)</h4><dl class="kv">${Object.entries(s.functions).map(([k, v]) => `<dt class="mono">${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl></div>
        <div class="card"><h4>Actions (governed writes)</h4><dl class="kv">${Object.entries(s.actions).map(([k, v]) => `<dt class="mono">${esc(k)}</dt><dd>${esc(v.description)}
          ${v.requires_approval ? ` <span class="chip warn">human approval</span>` : ""}${v.side_effect ? ` <span class="chip info">writes back: ${esc(v.side_effect)}</span>` : ""}</dd>`).join("")}</dl></div>
      </div></section>`;
  }

  function traceRows(spans) {
    if (!spans || !spans.length) return `<p class="meta">No trace exported for this run (one trace per diagnosis is kept).</p>`;
    const t0 = Math.min(...spans.map((s) => s.start)), t1 = Math.max(...spans.map((s) => s.end || s.start));
    const span = Math.max(t1 - t0, 0.001);
    const kids = {};
    spans.forEach((s) => (kids[s.parent_id] = kids[s.parent_id] || []).push(s));
    Object.values(kids).forEach((v) => v.sort((a, b) => a.start - b.start));
    const out = [];
    (function walk(pid, depth) {
      (kids[pid] || []).forEach((s) => {
        const left = ((s.start - t0) / span) * 100, width = Math.max(((s.end - s.start) / span) * 100, 0.6);
        const attrs = Object.entries(s.attributes).filter(([k]) => !["thread_id", "acting_for"].includes(k)).slice(0, 3).map(([k, v]) => `${k}=${v}`).join(" ");
        out.push(`<div class="trow ${s.status === "error" ? "err" : ""}"><div class="nm" style="padding-left:${8 + depth * 14}px" title="${esc(attrs)}">${esc(s.name)} <span style="color:var(--muted)">${s.duration_ms.toFixed(0)} ms ${esc(attrs)}</span></div><div class="bar"><i style="left:${left}%;width:${width}%"></i></div></div>`);
        s.events.forEach((e) => out.push(`<div class="trow ev"><div class="nm" style="padding-left:${22 + depth * 14}px">· ${esc(e.name)} ${esc(Object.entries(e.attributes).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ").slice(0, 140))}</div><div></div></div>`));
        walk(s.span_id, depth + 1);
      });
    })(null, 0);
    return `<div class="trace">${out.join("")}</div>`;
  }

  function runDetail(d, r) {
    const nar = esc(r.narrative || "").replace(/\[ev:([^\]]+)\]/g, (_, id) => `<span class="cite">${id}</span>`);
    const att = r.proposal?.attempts || [];
    return `<div class="card">
      <div class="eval-head"><h4>${pretty(r.diagnosis)} <span class="meta">confidence ${fmt(r.confidence)}</span></h4>${r.grounded ? `<span class="chip pass">grounded</span>` : `<span class="chip fail">guardrail rewrite</span>`}</div>
      <div class="meta">${Object.values(r.objects || {}).map(esc).join(" · ")} · ${esc(r.gt_key)}</div>
      <p class="narr">${nar}</p>
      <h3>What the agent tried</h3>
      <div class="steps">${att.map((a, i) => `${i ? `<span class="arrow">then</span>` : ""}<span class="mono">${esc(a.action)}</span> ${statusChip(a.status)}`).join(" ")}
        ${r.review ? `<span class="arrow">then</span> human review ${r.review.approved ? `<span class="chip pass">approved</span>` : `<span class="chip fail">rejected</span>`} <span class="mono">${esc(r.review.by)}</span>` : ""}
        <span class="arrow">result</span> ${statusChip(r.outcome?.status)}</div>
      ${att.filter((a) => a.status === "denied").map((a) => `<p class="meta">policy: ${esc(a.reason)}</p>`).join("")}
      ${r.outcome?.created ? `<p class="meta">created ${esc(r.outcome.created.type)} ${esc(r.outcome.created.id)}</p>` : ""}
      <h3>Trace ${r.trace_id ? `<span class="mono" style="font-weight:400">${esc(r.trace_id)}</span>` : ""}</h3>
      ${traceRows(d.traces[r.trace_id])}
      ${r.resume_trace_id ? `<h3>Resume after human review <span class="mono" style="font-weight:400">${esc(r.resume_trace_id)}</span> <span class="chip info">span link → ${esc(r.trace_id)}</span></h3>${traceRows(d.traces[r.resume_trace_id])}` : ""}
    </div>`;
  }

  function renderAgents(d) {
    return Object.values(d.agents || {}).map((uc) => {
      const firstWithTrace = uc.runs.findIndex((r) => d.traces[r.trace_id] && r.resume_trace_id) >= 0
        ? uc.runs.findIndex((r) => d.traces[r.trace_id] && r.resume_trace_id) : Math.max(0, uc.runs.findIndex((r) => d.traces[r.trace_id]));
      return `<section id="agents" style="--c:var(--l-app)"><h2><span class="layer">application</span>${esc(uc.title)}</h2>
        <p class="lede">${esc(uc.decision)} Agent <span class="mono">${esc(uc.agent)}</span> on LangGraph, LLM <span class="mono">${esc(uc.llm)}</span>. Rules decide, the model only narrates, and every claim cites evidence. Outbox: ${fmt(uc.outbox.delivered)} delivered to legacy, ${fmt(uc.outbox.failed_compensated)} compensated.</p>
        <div class="runs"><div class="card run-list" style="padding:0" role="listbox" aria-label="decisions">
          ${uc.runs.map((r, i) => `<button class="run-row" role="option" data-i="${i}" aria-selected="${i === firstWithTrace}">
            <span class="t">${esc(Object.values(r.objects || {})[0] || r.gt_key)}</span>${statusChip(r.outcome?.status)}
            <span class="d">${pretty(r.diagnosis)} · ${esc(r.proposal?.action || "")}</span><span class="d">${fmt(r.latency_ms)} ms</span></button>`).join("")}
        </div><div class="detail" data-uc="${esc(uc.usecase)}">${runDetail(d, uc.runs[firstWithTrace])}</div></div></section>`;
    }).join("");
  }

  function renderGov(d) {
    return `<section id="governance" style="--c:var(--accent)"><h2><span class="layer">governance</span>Policies and the audit trail</h2>
      <p class="lede">Cedar-style rules: default deny, forbid beats permit, checked when a tool is called, outside the model. Every action lands in an append-only, hash-chained audit log: chain <strong>${d.audit_chain_intact ? "intact" : "BROKEN"}</strong>.</p>
      <div class="grid g2"><div class="tbl"><table><thead><tr><th>Rule</th><th>Effect</th><th>Principal</th><th>Action</th><th>When</th></tr></thead><tbody>
        ${d.policies.map((p) => `<tr><td class="mono">${esc(p.id)}</td><td>${p.effect === "forbid" ? `<span class="chip fail">forbid</span>` : `<span class="chip pass">permit</span>`}</td><td class="mono">${esc(p.principal)}</td><td class="mono">${esc(p.action)}</td><td class="mono">${esc(p.when || "")}</td></tr>`).join("")}
      </tbody></table></div>
      <div class="tbl"><table><thead><tr><th class="num">Seq</th><th>Actor</th><th>Action</th><th>Status</th><th>Hash</th></tr></thead><tbody>
        ${d.audit.slice(0, 14).map((a) => `<tr><td class="num">${a.seq}</td><td class="mono">${esc(a.actor)}</td><td class="mono">${esc(a.action)}</td><td>${statusChip(a.status)}</td><td class="mono">${esc(String(a.hash).slice(0, 10))}</td></tr>`).join("")}
      </tbody></table></div></div></section>`;
  }

  function renderEvals(d) {
    const ev = d.evals;
    if (!ev) return "";
    const L = Object.fromEntries(ev.layers.map((l) => [l.layer, l]));
    const ag = L.agents?.metrics || {};
    const matrices = Object.entries(ag).map(([uc, m]) => {
      const labels = Object.keys(m.confusion);
      return `<div class="card"><h4>${pretty(uc)}: expected (rows) vs agent (columns)</h4><div class="tbl"><table class="matrix"><thead><tr><th></th>${labels.map((l) => `<th>${pretty(l)}</th>`).join("")}</tr></thead><tbody>
        ${labels.map((a) => `<tr><th>${pretty(a)}</th>${labels.map((b) => { const v = m.confusion[a][b]; return `<td class="${v ? (a === b ? "hit" : "miss") : ""}">${v || ""}</td>`; }).join("")}</tr>`).join("")}
        </tbody></table></div><dl class="kv" style="margin-top:10px"><dt>accuracy</dt><dd>${fmt(m.accuracy * 100)}%</dd><dt>grounded</dt><dd>${fmt(m.groundedness * 100)}%</dd>
        <dt>policy violations</dt><dd>${m.policy_violations}</dd><dt>denials that held</dt><dd>${m.policy_denials}</dd><dt>needed a human</dt><dd>${m.needed_human}</dd>
        <dt>p50 / p95 latency</dt><dd>${fmt(m.p50_latency_ms)} / ${fmt(m.p95_latency_ms)} ms</dd></dl></div>`;
    }).join("");
    const safety = L.actions?.tests || [];
    return `<section id="evals" style="--c:var(--pass)"><h2><span class="layer">evals</span>Every layer makes a promise; evals check it</h2>
      <p class="lede">Read bottom up: if the pipeline fails, an agent score means nothing. Ground truth comes from what the generator planted.</p>
      <div class="grid g3">${ev.layers.map((l) => `<div class="card"><div class="eval-head"><h4>${esc(l.layer)}</h4>${l.passed ? `<span class="chip pass">pass</span>` : `<span class="chip fail">fail</span>`}</div><p style="margin:6px 0 0;font-size:13.5px">${esc(l.headline)}</p></div>`).join("")}</div>
      <h3>Agent decisions vs ground truth</h3><div class="grid g2">${matrices}</div>
      <h3>Action safety tests (run on a scratch copy)</h3>
      <div class="tbl"><table><thead><tr><th>Test</th><th>Expected</th><th>Got</th><th></th></tr></thead><tbody>
        ${safety.map((t) => `<tr><td>${esc(t.test)}</td><td class="mono">${esc(t.expected)}</td><td class="mono">${esc(t.got)}</td><td>${t.passed ? `<span class="chip pass">pass</span>` : `<span class="chip fail">fail</span>`}</td></tr>`).join("")}
      </tbody></table></div></section>`;
  }

  function renderRun(d) {
    return `<section id="run" style="--c:var(--muted)"><h2><span class="layer">run it</span>Free on a laptop, then on AWS</h2>
      <div class="grid g2"><div class="card"><h4>Run every pack locally (no Docker, no keys, $0)</h4>
      <div class="code">pip install -e .
ironwright run --pack all
ironwright trace --pack ${esc(d.pack)}
ironwright serve --pack ${esc(d.pack)}     # REST API
ironwright mcp --pack ${esc(d.pack)}       # MCP tools for any agent
IRONWRIGHT_LLM=anthropic ironwright agents --pack ${esc(d.pack)}</div></div>
      <div class="card"><h4>How each piece maps to AWS</h4><dl class="aws">${Object.entries(d.aws_mapping || {}).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl></div></div></section>`;
  }

  async function show(id) {
    const root = $("#app");
    root.innerHTML = `<p class="meta" style="padding:30px 0">Loading ${esc(id)}…</p>`;
    let d;
    try { d = await load(id); } catch (e) { root.innerHTML = `<p class="card">Could not load data for ${esc(id)}: ${esc(e.message)}. Run <span class="mono">ironwright run --pack all</span> first.</p>`; return; }
    document.querySelectorAll(".pack-btn").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.id === id)));
    root.innerHTML = renderStrip(d) + renderStory(d) + renderLegacy(d) + renderLake(d) + renderRecon(d) + renderOntology(d) + renderAgents(d) + renderGov(d) + renderEvals(d) + renderRun(d);
    root.querySelectorAll(".run-list").forEach((list) => {
      const sel = list.querySelector('[aria-selected="true"]');
      if (sel) list.scrollTop = Math.max(0, sel.offsetTop - 60);
      const detail = list.parentElement.querySelector(".detail");
      const uc = Object.values(d.agents).find((u) => u.usecase === detail.dataset.uc);
      list.addEventListener("click", (e) => {
        const b = e.target.closest(".run-row"); if (!b) return;
        list.querySelectorAll(".run-row").forEach((x) => x.setAttribute("aria-selected", "false"));
        b.setAttribute("aria-selected", "true");
        detail.innerHTML = runDetail(d, uc.runs[+b.dataset.i]);
      });
    });
    try { localStorage.setItem("ironwright-pack", id); } catch (e) {}
  }

  async function init() {
    const bar = $("#packs");
    const statuses = await Promise.all(PACKS.map(async (p) => { try { const d = await load(p.id); return { ...p, title: d.title, ok: d.evals?.passed }; } catch (e) { return { ...p, title: p.short, ok: null }; } }));
    bar.innerHTML = statuses.map((p) => `<button class="pack-btn" data-id="${p.id}" aria-pressed="false"><small>${esc(p.short)}</small>${esc(p.title)} ${p.ok === true ? `<span class="chip pass" style="align-self:flex-start">evals pass</span>` : p.ok === false ? `<span class="chip fail" style="align-self:flex-start">evals fail</span>` : ""}</button>`).join("");
    bar.addEventListener("click", (e) => { const b = e.target.closest(".pack-btn"); if (b) show(b.dataset.id); });
    let start = (location.hash || "").replace("#", "");
    if (!PACKS.some((p) => p.id === start)) { try { start = localStorage.getItem("ironwright-pack") || "retail"; } catch (e) { start = "retail"; } }
    if (!PACKS.some((p) => p.id === start)) start = "retail";
    show(start);
  }
  init();
})();
