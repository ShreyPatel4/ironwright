import { useState } from "react";
import { api } from "../../api.js";
import { Badge, Err } from "../../components.jsx";
import { canWrite, isDone, JobLog, JobState, Panel, runPassed, useJob } from "./shared.jsx";

export default function RunStep({ journey, advance }) {
  const { workspace, runJob, me } = journey;
  const [startedId, setStartedId] = useState(null);
  const jobId = startedId ?? runJob?.id ?? null;
  const { job, log, err: pollErr } = useJob(jobId);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const writable = canWrite(me.role);

  async function start() {
    setBusy(true); setErr(null);
    try { setStartedId((await api.createJob("run", workspace.name)).id); } catch (e) { setErr(e); }
    setBusy(false);
  }
  async function cancel() {
    setErr(null);
    try { await api.cancelJob(jobId); } catch (e) { setErr(e); }
  }

  const layers = job?.result?.layers ?? [];
  const passed = runPassed(job);
  return (
    <>
      <h2>Run</h2>
      <p className="sub">Bronze, silver and gold, in the background. I check structure and reconciliation.
        There is no planted ground truth in your files, so the level stays L1 and I say so.</p>
      {!workspace && <p className="note">There is nothing to run until the workspace exists.</p>}
      {workspace && !jobId && !writable && <p className="note">Nobody has run this workspace yet.</p>}
      {workspace && !jobId && writable && (
        <button className="btn" disabled={busy} onClick={start} type="button">Run the pipeline</button>
      )}
      {jobId && (
        <Panel>
          <JobState job={job} />
          {job?.error && <Err e={job.error} />}
          <JobLog text={log} />
          {job?.result?.verification && (
            <div style={{ marginTop: 10 }}><Badge v={job.result.verification} /></div>
          )}
          {layers.map((l) => (
            <p key={l.layer} style={{ margin: "6px 0 0" }}>
              <span className={`badge ${l.passed ? "ok" : "bad"}`}>{l.layer}</span>{" "}
              <span className="note">{l.headline}</span>
            </p>
          ))}
          {writable && (
            <div className="row">
              {job && !isDone(job) && (
                <button className="btn ghost" onClick={cancel} type="button">Cancel</button>
              )}
              {isDone(job) && !passed && (
                <button className="btn" disabled={busy} onClick={start} type="button">Run again</button>
              )}
            </div>
          )}
        </Panel>
      )}
      <Err e={err || pollErr} />
      {job?.state === "succeeded" && !passed && (
        <p className="note">The run finished and a check failed, so the next step stays closed.</p>
      )}
      {passed && <button className="btn" onClick={() => advance()} type="button">Continue to Ontology</button>}
    </>
  );
}
