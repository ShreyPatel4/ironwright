import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

// The gold contract and what the bridge planner makes of it. Every status on this page
// comes from the server's rules. The page draws them and never decides one.
const STATUS_CLASS = { covered: "ok", partial: "hold", gap: "hold" };
const STATUS_WORD = { covered: "covered", partial: "partial", gap: "gap" };
const ROW = 22;
const TOP = 28;

function Lineage({ report }) {
  const left = [];
  for (const t of report.sources.tables) {
    for (const c of t.columns) left.push({ id: `${t.name}.${c.name}`, table: t.name, label: `${t.name}.${c.silver ?? c.name}` });
  }
  const right = report.columns.map((r) => ({ id: r.ref, row: r }));
  const ly = Object.fromEntries(left.map((n, i) => [n.id, TOP + i * ROW]));
  const height = TOP + Math.max(left.length, right.length) * ROW + 8;
  const width = 900;
  const lx = 210;
  const rx = 560;
  return (
    <div className="scroll" style={{ border: "1px solid var(--rule)", borderRadius: "var(--rad)", background: "var(--bg-1)" }}>
      <svg aria-label="lineage from source columns to gold columns" height={height} role="img"
           style={{ display: "block", font: "11px var(--font-mono)" }} viewBox={`0 0 ${width} ${height}`} width={width}>
        <text fill="var(--ink-2)" x={lx} y={14} textAnchor="end">sources</text>
        <text fill="var(--ink-2)" x={rx} y={14}>gold</text>
        {right.map((n, i) => {
          const r = n.row;
          if (!r.source) return null;
          const y1 = ly[`${r.source.table}.${r.source.column}`];
          const y2 = TOP + i * ROW;
          if (y1 === undefined) return null;
          const colour = r.status === "covered" ? "var(--success)" : "var(--warn)";
          return (
            <path d={`M${lx + 6},${y1 - 4} C${(lx + rx) / 2},${y1 - 4} ${(lx + rx) / 2},${y2 - 4} ${rx - 6},${y2 - 4}`}
                  data-status={r.status} fill="none" key={n.id} stroke={colour}
                  strokeDasharray={r.status === "covered" ? undefined : "4 3"} strokeWidth="1" />
          );
        })}
        {left.map((n) => (
          <text fill="var(--ink-1)" key={n.id} textAnchor="end" x={lx} y={ly[n.id]}>{n.label}</text>
        ))}
        {right.map((n, i) => (
          <g data-ref={n.id} data-status={n.row.status} key={n.id}>
            <text fill={n.row.status === "covered" ? "var(--ink-0)" : "var(--warn)"} x={rx} y={TOP + i * ROW}>
              {n.id}{n.row.status === "covered" ? "" : `  ${STATUS_WORD[n.row.status]}`}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

function Missing({ r }) {
  if (r.status === "gap") return <span className="note">{r.reason}</span>;
  if (!r.missing.length) return <span className="note">{r.reason}</span>;
  return <span className="note">{r.missing.map((m) => `${m.kind}: ${m.detail}`).join(" ")}</span>;
}

export default function Gold({ ws }) {
  const [text, setText] = useState("");
  const [saved, setSaved] = useState(false);
  const [report, setReport] = useState(null);
  const [asks, setAsks] = useState("");
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  async function load() {
    setErr(null);
    try {
      const g = await api.gold(ws.name);
      setText(g.yaml); setSaved(true);
      setReport(await api.bridgePlan(ws.name));
    } catch (e) {
      if (e.status === 404) { setSaved(false); setReport(null); if (!/no gold contract yet/.test(e.message)) setErr(e); }
      else setErr(e);
    }
  }

  useEffect(() => { setText(""); setDraft(null); setReport(null); load(); }, [ws.name]);

  async function save() {
    setBusy(true); setErr(null);
    try { await api.putGold(ws.name, text); await load(); } catch (e) { setErr(e); }
    setBusy(false);
  }

  async function makeDraft() {
    setBusy(true); setErr(null);
    try {
      const got = await api.draftGold(ws.name, asks.split("\n").map((s) => s.trim()).filter(Boolean));
      setDraft(got); setText(got.yaml);
    } catch (e) { setErr(e); }
    setBusy(false);
  }

  const s = report?.summary;
  return (
    <>
      <h2>Gold</h2>
      <p className="sub">Tell me what the gold layer and its marts must look like. I work backward
        from every mart column to a column in your files and show what is covered, what is partly
        there, and what is missing. Rules decide each status. A model may only draft the contract
        for you to edit.</p>
      <Err e={err} />

      <h3>Draft from your use cases</h3>
      <p className="note">One question per line. The draft is not saved until you save it below.</p>
      <textarea aria-label="use cases" onChange={(e) => setAsks(e.target.value)}
                placeholder="Which open claims are paid above policy limit, by adjuster, weekly?" value={asks} />
      <div className="row">
        <button className="btn ghost" disabled={busy || !asks.trim()} onClick={makeDraft}>Draft a contract</button>
        {draft && <span className={`badge ${draft.valid ? "ok" : "hold"}`}>
          {draft.valid ? "the draft validates" : "the draft needs work"}</span>}
      </div>
      {draft && <p className="note">{draft.note}{draft.errors.length ? ` ${draft.errors[0]}` : ""}</p>}

      <h3>The contract</h3>
      <textarea aria-label="gold contract" className="mono" onChange={(e) => setText(e.target.value)}
                placeholder="kind: gold-contract" spellCheck={false} style={{ minHeight: 280 }} value={text} />
      <div className="row">
        <button className="btn" disabled={busy || !text.trim()} onClick={save}>Save contract</button>
        <span className="note">{saved ? "saved as gold.yaml in this workspace" : "nothing saved yet"}</span>
      </div>

      {report && (
        <>
          <h3>Coverage</h3>
          <div className="row">
            <span className="badge ok">{s.gold.covered} covered</span>
            <span className="badge hold">{s.gold.partial} partial</span>
            <span className="badge hold">{s.gold.gap} gaps</span>
            <span className="note mono">gold coverage {Math.round(s.gold.coverage * 100)}% of {s.gold.total} columns</span>
          </div>
          <Lineage report={report} />
          <div className="scroll"><table>
            <thead><tr><th>gold column</th><th>status</th><th>source</th><th>rule</th><th>what is missing</th></tr></thead>
            <tbody>
              {report.columns.map((r) => (
                <tr data-ref={r.ref} key={r.ref}>
                  <td className="mono">{r.ref}</td>
                  <td><span className={`badge ${STATUS_CLASS[r.status]}`}>{STATUS_WORD[r.status]}</span></td>
                  <td className="mono">{r.source ? `${r.source.table}.${r.source.column}` : "none"}</td>
                  <td className="mono">{r.rule ?? ""}</td>
                  <td><Missing r={r} /></td>
                </tr>
              ))}
            </tbody>
          </table></div>
          {report.marts.map((m) => (
            <div key={m.name}>
              <h3>{m.name}</h3>
              <p className="note">{m.grain}. {m.summary.covered} of {m.summary.total} columns covered.
                {m.joins.filter((j) => !j.ok).map((j) => ` ${j.reason}`).join("")}</p>
            </div>
          ))}
        </>
      )}
    </>
  );
}
