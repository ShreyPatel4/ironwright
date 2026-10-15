import { useState } from "react";
import { api } from "../api.js";
import { Badge, Err } from "../components.jsx";

const STAGES = ["generate", "migrate", "reconcile", "ontology", "agents", "evals", "export"];

export default function Pipeline({ ws, refresh }) {
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  async function run() {
    setBusy(true); setErr(null); setReport(null);
    try {
      setReport(await api.run(ws.name, {}));
      await refresh();
    } catch (e) { setErr(e); }
    setBusy(false);
  }

  const done = Object.fromEntries((report?.stages ?? []).map((s) => [s.stage, s]));

  return (
    <>
      <h2>Pipeline</h2>
      <p className="sub">The real stages, run in order on this machine: legacy generation, landing
        with write-audit-publish, reconciliation, ontology build, agents, evals, export. Timings
        are measured, not estimated.</p>
      <div aria-live="polite" className="row">
        <button className="btn" disabled={busy} onClick={run}>
          {busy ? "Running…" : "Run all stages"}
        </button>
        <Badge v={report?.verification ?? ws.verification} />
        {report?.evals && (
          <span className={`badge ${report.evals.passed ? "ok" : "bad"}`}>
            evals {report.evals.passed ? "green" : "RED"}
          </span>
        )}
      </div>
      <Err e={err} />
      <div className="stage-row">
        {STAGES.map((s) => {
          const st = done[s];
          return (
            <div className={`stage ${st ? "ok" : busy ? "run" : ""}`} key={s}>
              <small>{s}</small>
              <span className="t">{st ? `${st.seconds}s` : busy ? "…" : "—"}</span>
            </div>
          );
        })}
      </div>
      {!report && <p className="note">Nothing has run in this view yet. The verification badge
        above is derived from what is actually on disk.</p>}
    </>
  );
}
