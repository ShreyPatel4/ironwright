import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

/* The platform map: the whole concept on one canvas, Foundry-style, with
   the house rule enforced by the server and its test: LIVE nodes open the
   real surface, PLANNED nodes are drawn as working-drawing outlines and
   open a spec panel. Nothing on this canvas pretends. */

const LANE_LABELS = {
  sources: "SOURCES", pipeline: "PIPELINE", lakehouse: "LAKEHOUSE",
  semantic: "SEMANTIC", operate: "OPERATE", govern: "GOVERN",
};
const NODE_W = 172;
const NODE_H = 46;
const LANE_W = 196;
const GAP_Y = 12;

// Views the console only offers for one workspace kind; opening a pack-only
// surface from an organization workspace would 404 on every call.
const PACK_ONLY = new Set(["data", "pipeline", "loads", "explorer", "semantic",
                           "discovery", "ask", "boards", "agents", "approvals",
                           "audit", "governance", "principals"]);
const ORG_ONLY = new Set(["organization", "orgpolicy"]);

export default function Platform({ setView, ws }) {
  const [map, setMap] = useState(null);
  const [sel, setSel] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => { api.platformMap().then(setMap).catch(setErr); }, []);

  if (err) return <Err e={err} />;
  if (!map) return <p className="note">Loading the map…</p>;

  const byLane = {};
  map.nodes.forEach((n) => { (byLane[n.lane] ||= []).push(n); });
  const laneX = {};
  map.lanes.forEach((l, i) => { laneX[l] = 16 + i * LANE_W; });
  const pos = {};
  map.lanes.forEach((l) => {
    (byLane[l] ?? []).forEach((n, i) => {
      pos[n.id] = { x: laneX[l], y: 44 + i * (NODE_H + GAP_Y) };
    });
  });
  const height = 60 + Math.max(...map.lanes.map((l) => (byLane[l] ?? []).length)) * (NODE_H + GAP_Y);
  const width = 16 + map.lanes.length * LANE_W;

  return (
    <>
      <h2>Platform</h2>
      <p className="sub">{map.note}</p>
      <div className="split" style={{ gridTemplateColumns: "minmax(0,1fr) 300px" }}>
        <div className="scroll" style={{ background: "var(--bg-1)", border: "1px solid var(--rule)",
                                         borderRadius: "var(--rad)" }}>
          <svg height={height} role="list" width={width}>
            {map.lanes.map((l) => (
              <g key={l}>
                <text fill="var(--ink-2)" fontFamily="Geist Mono, monospace" fontSize="9.5"
                      letterSpacing="1.4" x={laneX[l]} y={26}>{LANE_LABELS[l]}</text>
                <line stroke="var(--hair)" x1={laneX[l] - 10} x2={laneX[l] - 10} y1={14} y2={height - 10} />
              </g>
            ))}
            {/* dependency edges, planned node -> its dependency */}
            {map.nodes.filter((n) => n.depends_on && pos[n.depends_on]).map((n) => {
              const a = pos[n.depends_on]; const b = pos[n.id];
              return (
                <path d={`M ${a.x + NODE_W} ${a.y + NODE_H / 2} C ${a.x + NODE_W + 28} ${a.y + NODE_H / 2}, ${b.x - 28} ${b.y + NODE_H / 2}, ${b.x} ${b.y + NODE_H / 2}`}
                      fill="none" key={`${n.id}-edge`} stroke="var(--rule)" strokeDasharray="3 4" />
              );
            })}
            {map.nodes.map((n) => {
              const p = pos[n.id];
              const selected = sel?.id === n.id;
              return (
                <g aria-label={`${n.label}, ${n.status === "live" ? "live" : "planned"}`}
                   className="canvas-node" cursor="pointer" key={n.id}
                   onClick={() => setSel(n)}
                   onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), setSel(n))}
                   role="button" tabIndex={0}>
                  <rect fill={n.status === "live" ? "var(--bg-0)" : "var(--bg-1)"}
                        height={NODE_H} rx="2"
                        stroke={selected ? "var(--accent)" : n.status === "live" ? "var(--ink-1)" : "var(--rule)"}
                        strokeDasharray={n.status === "live" ? "" : "4 3"}
                        width={NODE_W} x={p.x} y={p.y} />
                  <circle cx={p.x + 12} cy={p.y + 15} fill={n.status === "live" ? "var(--success)" : "var(--ink-2)"} r="2.6" />
                  <text fill="var(--ink-0)" fontFamily="Geist, sans-serif" fontSize="10.5"
                        fontWeight="500" x={p.x + 22} y={p.y + 18}>
                    {n.label.length > 28 ? `${n.label.slice(0, 27)}…` : n.label}
                  </text>
                  <text fill="var(--ink-2)" fontFamily="Geist Mono, monospace" fontSize="8.5"
                        letterSpacing="1" x={p.x + 22} y={p.y + 34}>
                    {n.status === "live" ? "LIVE" : "PLANNED"}
                  </text>
                </g>
              );
            })}
          </svg>
        </div>
        <div>
          {!sel && <p className="note">Select a node. Solid outline with a green mark is live;
            a dashed outline is a planned capability with its dependency drawn in.</p>}
          {sel && (
            <>
              <h3 style={{ marginTop: 0 }}>{sel.label}</h3>
              <p className={`badge ${sel.status === "live" ? "ok" : "hold"}`}>
                {sel.status === "live" ? "live on this machine" : "planned, not posed"}
              </p>
              <p style={{ fontSize: 13.5, lineHeight: 1.65 }}>{sel.detail}</p>
              {sel.status === "live" ? (
                (ws?.kind === "organization" ? PACK_ONLY : ORG_ONLY).has(sel.view) ? (
                  <p className="note">
                    This surface lives on a {ws?.kind === "organization"
                      ? "demo company workspace; switch to one in the rail to open it"
                      : "created organization; create or open one from Workspaces"}.
                  </p>
                ) : (
                  <button className="btn" onClick={() => setView(sel.view)}>Open the surface</button>
                )
              ) : (
                <p className="note">
                  depends on: <span className="mono">{sel.depends_on ?? "nothing in the map"}</span>
                  <br />Longhand in docs/platform-vision.md.
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}
