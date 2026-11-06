import { useEffect, useRef, useState } from "react";
import { api } from "../../api.js";

/* What every step shares. The journey comes from the server each time, so a new
   browser lands on the same step as the old one. */

export const STEPS = [
  ["account", "Account"],
  ["upload", "Upload"],
  ["profile", "Profile"],
  ["map", "Map"],
  ["run", "Run"],
  ["ontology", "Ontology"],
  ["actions", "Actions"],
  ["ask", "Ask"],
];

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const POLL_MS = 1000;

export const isDone = (job) => !!job && TERMINAL.has(job.state);
export const canWrite = (role) => role !== "viewer";
export const canAdmin = (role) => role === "owner" || role === "admin";

// A run that finished with a failed eval did not pass. Evals gate everything.
export const runPassed = (job) => job?.state === "succeeded" && job.result?.evals?.passed === true;

export async function loadJourney() {
  const [me, uploads, workspaces] = await Promise.all([api.me(), api.uploads(), api.workspaces()]);
  const upload = uploads[0] ?? null;
  const workspace = workspaces.filter((w) => w.kind === "workspace").at(-1) ?? null;
  const workspaceJobs = workspace ? await api.jobs(workspace.name) : [];
  // A workspace with no ontology yet may answer this with an error. Either way
  // nothing is accepted, and the Ontology step reports its own failures.
  const ontology = workspace ? await api.ontology(workspace.name).catch(() => null) : null;
  return {
    me, uploads, upload, workspace,
    profileJob: upload?.profile_job ?? null,
    runJob: workspaceJobs.find((j) => j.kind === "run") ?? null,
    proposeJob: workspaceJobs.find((j) => j.kind === "ontology_propose") ?? null,
    ontologyAccepted: Object.keys(ontology?.describe?.object_types ?? {}).length > 0,
  };
}

export function furthestStep(j) {
  if (j.workspace) {
    if (!runPassed(j.runJob)) return "run";
    return j.ontologyAccepted ? "ask" : "ontology";
  }
  if (!j.upload) return "upload";
  return j.profileJob?.state === "succeeded" ? "map" : "profile";
}

export function Panel({ children, style }) {
  return (
    <div data-testid="wizard-panel"
         style={{ background: "var(--bg-1)", border: "1px solid var(--rule)", borderRadius: "var(--rad)",
                  marginBottom: 14, padding: "14px 16px", ...style }}>
      {children}
    </div>
  );
}

export const Loading = ({ what }) => <p className="note">Loading {what}.</p>;

/* Follows one job to its end: its state, and its log from a byte offset. */
export function useJob(jobId) {
  const [job, setJob] = useState(null);
  const [log, setLog] = useState("");
  const [err, setErr] = useState(null);
  const offset = useRef(0);

  useEffect(() => {
    setJob(null); setLog(""); setErr(null);
    offset.current = 0;
    if (!jobId) return undefined;
    let stopped = false;
    let timer = null;

    async function drainLog() {
      for (;;) {
        const tail = await api.jobLog(jobId, offset.current);
        if (stopped || !tail.data) return;
        offset.current = tail.next_offset;
        setLog((l) => l + tail.data);
      }
    }

    async function tick() {
      try {
        // The state first: a log read after a finished job holds its last line.
        const now = await api.job(jobId);
        if (stopped) return;
        setJob(now);
        await drainLog();
        setErr(null);
        if (!stopped && !isDone(now)) timer = setTimeout(tick, POLL_MS);
      } catch (e) {
        if (!stopped) { setErr(e); timer = setTimeout(tick, POLL_MS * 3); }
      }
    }
    tick();
    return () => { stopped = true; clearTimeout(timer); };
  }, [jobId]);

  return { job, log, err };
}

const STATE_BADGE = { succeeded: "ok", failed: "bad", cancelled: "hold" };

export function JobState({ job }) {
  if (!job) return <p className="note">Loading the job.</p>;
  return (
    <div aria-live="polite" className="row">
      <span className={`badge ${STATE_BADGE[job.state] ?? ""}`}>{job.state}</span>
      {job.state === "queued" && (
        <span className="note">It is in the queue, waiting for a worker to pick it up.</span>
      )}
      {job.state === "running" && <span className="note">A worker has it. The log below is live.</span>}
    </div>
  );
}

export function JobLog({ text }) {
  return (
    <pre aria-label="job log" className="mono"
         style={{ background: "var(--bg-2)", border: "1px solid var(--rule)", borderRadius: "var(--rad)",
                  margin: "10px 0 0", maxHeight: 220, minHeight: 44, overflow: "auto", padding: "8px 10px",
                  whiteSpace: "pre-wrap" }}>
      {text || "Nothing in the log yet."}
    </pre>
  );
}
