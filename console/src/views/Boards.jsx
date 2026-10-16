import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { Err, Rows } from "../components.jsx";

/* Boards: tiles over the lakehouse. Every tile carries its SQL (one click
   away), its viz and its last run time; a stale tile says when it ran
   instead of posing as current. Hand-rolled SVG for bar/line/number. */

function Chart({ tile, result }) {
  if (!result) return <p className="note">not run yet</p>;
  if (!result.ok) return <p className="err">{result.error}</p>;
  const { columns, rows } = result;
  if (tile.viz === "number") {
    return <p style={{ font: "500 34px/1.2 var(--font-mono)", margin: "6px 0" }}>
      {rows[0]?.[0] ?? "∅"}</p>;
  }
  if (tile.viz === "bar" || tile.viz === "line") {
    const pts = rows.map((r) => [r[0], Number(r[1])]).filter((p) => Number.isFinite(p[1]));
    if (!pts.length) return <p className="note">no numeric rows to draw</p>;
    const max = Math.max(...pts.map((p) => p[1]), 0);
    const W = 300; const H = 120; const bw = W / pts.length;
    return (
      <svg aria-label={`${tile.title}: ${columns[1]} by ${columns[0]}`} height={H + 18} width={W}>
        {tile.viz === "bar" && pts.map(([label, v], i) => (
          <rect fill="var(--accent)" height={max ? (v / max) * H : 0} key={i}
                width={Math.max(bw - 3, 2)} x={i * bw}
                y={H - (max ? (v / max) * H : 0)}>
            <title>{`${label}: ${v}`}</title>
          </rect>
        ))}
        {tile.viz === "line" && (
          <polyline fill="none"
                    points={pts.map(([, v], i) => `${i * bw + bw / 2},${H - (max ? (v / max) * H : 0)}`).join(" ")}
                    stroke="var(--accent)" strokeWidth="2" />
        )}
        <text fill="var(--ink-2)" fontFamily="Geist Mono, monospace" fontSize="9"
              x="0" y={H + 13}>{pts[0][0]}</text>
        <text fill="var(--ink-2)" fontFamily="Geist Mono, monospace" fontSize="9"
              textAnchor="end" x={W} y={H + 13}>{pts[pts.length - 1][0]}</text>
      </svg>
    );
  }
  return <Rows columns={columns} rows={rows.slice(0, 8)} />;
}

const STARTER = (gold) => ([
  { title: "Row count", sql: `select count(*) as n from ${gold}`, viz: "number" },
  { title: "Sample", sql: `select * from ${gold} limit 5`, viz: "table" },
]);

export default function Boards({ ws }) {
  const [boards, setBoards] = useState([]);
  const [current, setCurrent] = useState(null);
  const [results, setResults] = useState(null);
  const [newSlug, setNewSlug] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(() => {
    api.boards(ws.name).then(setBoards).catch(setErr);
  }, [ws.name]);
  useEffect(() => { setCurrent(null); setResults(null); setErr(null); load(); }, [load]);

  async function open(slug) {
    setErr(null); setResults(null);
    try { setCurrent(await api.board(ws.name, slug)); } catch (e) { setErr(e); }
  }

  async function create() {
    setErr(null);
    try {
      const tables = await api.lakeTables(ws.name);
      const gold = tables.find((t) => t.layer === "gold");
      const tiles = STARTER(gold ? `${gold.layer}_${gold.name}` : "bronze_" + tables[0].name);
      const b = await api.saveBoard(ws.name, newSlug, { title: newSlug, tiles });
      setNewSlug(""); load(); setCurrent(b);
    } catch (e) { setErr(e); }
  }

  async function runAll() {
    setBusy(true); setErr(null);
    try { setResults(await api.runBoard(ws.name, current.slug)); } catch (e) { setErr(e); }
    setBusy(false);
  }

  async function updateTile(i, patch) {
    const tiles = current.tiles.map((t, j) => (j === i ? { ...t, ...patch } : t));
    try {
      setCurrent(await api.saveBoard(ws.name, current.slug, { title: current.title, tiles }));
      setErr(null);
    } catch (e) { setErr(e); }
  }

  async function addTile() {
    const tiles = [...current.tiles, { title: `Tile ${current.tiles.length + 1}`,
                                       sql: "select 1 as n", viz: "number" }];
    setCurrent(await api.saveBoard(ws.name, current.slug, { title: current.title, tiles }));
  }

  const byId = Object.fromEntries((results?.tiles ?? []).map((t) => [t.id, t]));

  return (
    <>
      <h2>Boards</h2>
      <p className="sub">The built-in BI: tiles over this workspace's lakehouse. Every tile is a
        single SELECT (anything else is rejected when you save it), shows its SQL one click
        away, and says when it last ran instead of posing as current.</p>
      <Err e={err} />
      {!current && (
        <>
          {boards.length === 0 && <p className="note">No boards yet in this workspace.</p>}
          <div className="cards" style={{ marginBottom: 14, maxWidth: 560 }}>
            {boards.map((b) => (
              <button className="card" key={b.slug} onClick={() => open(b.slug)}
                      style={{ gridTemplateColumns: "minmax(0,14rem) 1fr" }}>
                <b>{b.title}</b><p>{b.tiles} tiles</p>
              </button>
            ))}
          </div>
          <div className="row">
            <label className="note" htmlFor="board-slug">new board</label>
            <input id="board-slug" onChange={(e) => setNewSlug(e.target.value)}
                   placeholder="slug, e.g. weekly-ops" type="text" value={newSlug} />
            <button className="btn" disabled={!newSlug.trim()} onClick={create}>
              Create with starter tiles</button>
          </div>
        </>
      )}
      {current && (
        <>
          <div className="row">
            <button className="btn ghost" onClick={() => { setCurrent(null); setResults(null); load(); }}>
              ← all boards</button>
            <b>{current.title}</b>
            <button className="btn" disabled={busy} onClick={runAll}>
              {busy ? "Running…" : "Run all tiles"}</button>
            {results && <span className="note">ran {new Date(results.ran_at * 1000).toLocaleTimeString()}</span>}
            <button className="btn ghost" onClick={addTile}>Add tile</button>
            <button className="btn danger"
                    onClick={async () => { await api.deleteBoard(ws.name, current.slug); setCurrent(null); load(); }}>
              Delete board</button>
          </div>
          <div aria-live="polite"
               style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fill,minmax(330px,1fr))" }}>
            {current.tiles.map((t, i) => (
              <div key={t.id} style={{ background: "var(--bg-1)", border: "1px solid var(--rule)",
                                       borderRadius: "var(--rad)", padding: "12px 14px" }}>
                <div className="row" style={{ margin: 0 }}>
                  <input aria-label="tile title" className="mono"
                         onChange={(e) => updateTile(i, { title: e.target.value })}
                         style={{ border: 0, flex: 1, padding: 0 }} type="text" value={t.title} />
                  <select aria-label="tile viz" onChange={(e) => updateTile(i, { viz: e.target.value })}
                          value={t.viz}>
                    {["number", "bar", "line", "table"].map((v) => <option key={v}>{v}</option>)}
                  </select>
                </div>
                <Chart result={byId[t.id]} tile={t} />
                <details>
                  <summary className="note" style={{ cursor: "pointer" }}>sql</summary>
                  <textarea aria-label={`SQL for ${t.title}`}
                            defaultValue={t.sql}
                            onBlur={(e) => e.target.value !== t.sql && updateTile(i, { sql: e.target.value })}
                            spellCheck={false} style={{ minHeight: 64 }} />
                </details>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}
