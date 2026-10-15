import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

export default function Agents({ ws }) {
  const [agents, setAgents] = useState(null);
  const [uc, setUc] = useState(null);
  const [run, setRun] = useState(null);
  const [spans, setSpans] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    setAgents(null); setUc(null); setRun(null); setSpans(null); setErr(null);
    api.agents(ws.name).then((a) => {
      setAgents(a);
      setUc(Object.keys(a)[0] ?? null);
    }).catch(setErr);
  }, [ws.name]);

  async function investigate(r) {
    setRun(r); setSpans(null); setErr(null);
    try { setSpans(await api.trace(ws.name, r.trace_id)); } catch (e) { setErr(e); }
  }

  const runs = (uc && agents?.[uc]?.runs) || [];
  const t0 = spans?.length ? Math.min(...spans.map((s) => s.start)) : 0;
  const t1 = spans?.length ? Math.max(...spans.map((s) => s.end ?? s.start)) : 1;

  return (
    <>
      <h2>Agents</h2>
      <p className="sub">Every decision an agent took, with its evidence and its trace. Rules
        decide and the model narrates; a narrative may only cite evidence ids the agent actually
        gathered. Pick a decision to investigate its full span waterfall.</p>
      <Err e={err} />
      {agents && (
        <div className="row">
          {Object.keys(agents).map((k) => (
            <button aria-pressed={k === uc} className="btn ghost" key={k}
                    onClick={() => { setUc(k); setRun(null); setSpans(null); }}>
              {k} · {agents[k].runs.length} decisions
            </button>
          ))}
        </div>
      )}
      {!run && runs.length > 0 && (
        <div className="scroll"><table>
          <thead><tr><th>target</th><th>diagnosis</th><th>confidence</th><th>outcome</th><th>grounded</th><th></th></tr></thead>
          <tbody>
            {runs.map((r, i) => (
              <tr key={i}>
                <td className="mono">{JSON.stringify(r.target)}</td>
                <td className="mono">{r.diagnosis}</td>
                <td className="mono">{r.confidence}</td>
                <td>{(() => {
                  const o = r.outcome && typeof r.outcome === "object" ? r.outcome.status : r.outcome;
                  return <span className={`badge ${o === "executed" || o === "applied" ? "ok" : "hold"}`}>{String(o)}</span>;
                })()}</td>
                <td><span className={`badge ${r.grounded ? "ok" : "bad"}`}>{r.grounded ? "cited" : "UNGROUNDED"}</span></td>
                <td><button className="btn ghost" onClick={() => investigate(r)}>Investigate</button></td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
      {run && (
        <>
          <div className="row">
            <button className="btn ghost" onClick={() => { setRun(null); setSpans(null); }}>← all decisions</button>
            <span className="mono note">trace {run.trace_id}</span>
          </div>
          <h3>Diagnosis: {run.diagnosis} ({run.confidence})</h3>
          <pre className="mono" style={{ whiteSpace: "pre-wrap" }}>{run.narrative}</pre>
          <p className="note">evidence cited: {(run.evidence_ids ?? []).join(", ") || "none"}</p>
          {run.proposal && <p className="note">proposal: <span className="mono">{JSON.stringify(run.proposal)}</span></p>}
          {run.outcome && typeof run.outcome === "object" && (
            <p className="note">outcome: <span className="mono">{JSON.stringify(run.outcome)}</span></p>
          )}
          {spans && (
            <>
              <h3>Trace waterfall ({spans.length} spans)</h3>
              <div className="scroll"><table>
                <thead><tr><th style={{ width: "34%" }}>span</th><th>timeline</th><th>ms</th></tr></thead>
                <tbody>
                  {spans.map((s, i) => {
                    const left = ((s.start - t0) / Math.max(t1 - t0, 1e-9)) * 100;
                    const width = Math.max((((s.end ?? s.start) - s.start) / Math.max(t1 - t0, 1e-9)) * 100, 0.5);
                    return (
                      <tr key={i}>
                        <td className="mono">{s.name}</td>
                        <td>
                          <div style={{ background: "var(--sunk)", borderRadius: 3, height: 10, position: "relative" }}>
                            <div style={{ background: "var(--accent)", borderRadius: 3, height: 10,
                                          left: `${left}%`, position: "absolute", width: `${width}%` }} />
                          </div>
                        </td>
                        <td className="mono">{Math.round(((s.end ?? s.start) - s.start) * 1000)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table></div>
            </>
          )}
        </>
      )}
    </>
  );
}
