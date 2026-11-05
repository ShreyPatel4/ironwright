import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge, Err, ReadOnlyNote } from "../components.jsx";

const STAGES = ["generate", "migrate", "reconcile", "ontology", "agents", "evals", "export"];
const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

const pause = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

// A workspace from your own files runs as one job with no stages to time. The job is the
// record, so what ran last comes from the server, whichever screen started it.
function LastRun({ job }) {
  const evals = job.result?.evals;
  return (
    <>
      <div aria-live="polite" className="row">
        <span className="mono note">last run {job.state}</span>
        {job.finished_at && <span className="note">{new Date(job.finished_at).toLocaleString()}</span>}
        {evals && <span className={`badge ${evals.passed ? "ok" : "bad"}`}>evals {evals.passed ? "green" : "RED"}</span>}
      </div>
      {job.error && <p className="note">{job.error}</p>}
      {job.result?.layers && (
        <div className="scroll"><table>
          <thead><tr><th>layer</th><th>result</th><th>what it found</th></tr></thead>
          <tbody>
            {job.result.layers.map((l) => (
              <tr key={l.layer}>
                <td className="mono">{l.layer}</td>
                <td><span className={`badge ${l.passed ? "ok" : "bad"}`}>{l.passed ? "pass" : "fail"}</span></td>
                <td>{l.headline}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </>
  );
}

function TenantPipeline({ ws, refresh }) {
  const [job, setJob] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    const jobs = await api.jobs(ws.name);
    setJob(jobs.find((j) => j.kind === "run") ?? null);
    setLoaded(true);
  }, [ws.name]);
  useEffect(() => { load().catch(setErr); }, [load]);

  async function run() {
    setBusy(true); setErr(null);
    try {
      const started = await api.run(ws.name, {});
      let state = started.state;
      while (!TERMINAL.has(state)) {
        await pause(1000);
        const now = await api.job(started.id);
        setJob(now);
        state = now.state;
      }
      await refresh();
    } catch (e) { setErr(e); }
    await load().catch(setErr);
    setBusy(false);
  }

  return (
    <>
      <h2>Pipeline</h2>
      <p className="sub">Your files through landing with write-audit-publish, reconciliation, the
        ontology build and the checks. It runs as one job, with no generated data and no agents.</p>
      <div className="row">
        <button className="btn" disabled={busy} onClick={run}>{busy ? "Running…" : "Run the pipeline"}</button>
        <Badge v={ws.verification} />
      </div>
      <Err e={err} />
      {job && <LastRun job={job} />}
      {loaded && !job && <p className="note">Nothing has run on this workspace yet.</p>}
    </>
  );
}

function PackPipeline({ ws, refresh }) {
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

  // POST /runs is for a pack this caller may write to. A tenant workspace runs as a job.
  const canRun = ws.kind === "pack" && !ws.read_only;
  const done = Object.fromEntries((report?.stages ?? []).map((s) => [s.stage, s]));

  return (
    <>
      <h2>Pipeline</h2>
      <p className="sub">The real stages, run in order on this machine: legacy generation, landing
        with write-audit-publish, reconciliation, ontology build, agents, evals, export. Timings
        are measured, not estimated.</p>
      <ReadOnlyNote ws={ws} />
      {ws.kind !== "pack" && <p className="note">This workspace runs from its setup flow, not from here.</p>}
      <div aria-live="polite" className="row">
        {canRun && (
          <button className="btn" disabled={busy} onClick={run}>
            {busy ? "Running…" : "Run all stages"}
          </button>
        )}
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

export default function Pipeline(props) {
  return props.ws.kind === "workspace" ? <TenantPipeline {...props} /> : <PackPipeline {...props} />;
}
