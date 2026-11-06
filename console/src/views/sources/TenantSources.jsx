import { useCallback, useEffect, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";
import { canAdmin, canWrite, isDone, JobLog, JobState, Panel, runPassed, useJob } from "../onboarding/shared.jsx";

/* Your own workspace: refresh a file, read what changed, decide, run again. Every severity
   comes from the server. The server also refuses a run on blocking drift, so the closed
   button here is only manners. */

const ACCEPT = ".csv,.txt,.dat,.tsv";
const TYPES = ["string", "int", "bigint", "double", "boolean", "date", "timestamp"];
const IDENT = /^[a-z][a-z0-9_]{0,62}$/;
const SEVERITY_BADGE = { block: "bad", warn: "hold", info: "" };

function DriftReport({ name, report }) {
  return (
    <section aria-label={`drift for ${name}`} style={{ marginTop: 10 }}>
      {report.items.length === 0 && (
        <p className="note" style={{ margin: 0 }}>
          {report.against_version ? "This version did not change shape." : "This is the first version, so there is nothing to compare it with."}
        </p>
      )}
      {report.items.map((item) => (
        <p key={`${item.kind}:${item.column}`} style={{ margin: "4px 0" }}>
          <span className={`badge ${SEVERITY_BADGE[item.severity] ?? ""}`}>{item.severity}</span>{" "}
          {item.message}
          {item.ontology.length > 0 && (
            <span className="note"> Your ontology reads this: {item.ontology.map((o) => o.name).join(", ")}.</span>
          )}
        </p>
      ))}
    </section>
  );
}

/* The answer to blocking drift. A new column in the file starts unticked, because the
   person decides what the workspace reads. Fixed width has no header, so its layout moves too. */
function specRows(shape, report) {
  const declared = shape.columns.map((c) => ({ name: c.name, type: c.type, key: !!c.key, keep: true }));
  const fresh = report.items.filter((i) => i.kind === "new_column")
    .map((i) => ({ name: i.column, type: "string", key: false, keep: false, start: i.start, width: i.width }));
  return [...declared, ...fresh];
}

function specBody(shape, rows) {
  const kept = rows.filter((r) => r.keep);
  const columns = kept.map((r) => ({ name: r.name, type: r.type, ...(r.key ? { key: true } : {}) }));
  if (shape.format !== "fixed_width") return { columns };
  const added = kept.filter((r) => r.start !== undefined).map(({ name, start, width }) => ({ name, start, width }));
  const fields = shape.layout.fields.filter((f) => kept.some((r) => r.name === f.name));
  return { columns, layout: { ...shape.layout, fields: [...fields, ...added] } };
}

function SpecEditor({ name, ws, shape, report, onSaved }) {
  const [rows, setRows] = useState(() => specRows(shape, report));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const set = (i, patch) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const dropped = rows.filter((r) => !r.keep && r.start === undefined && shape.columns.some((c) => c.name === r.name));

  async function save() {
    setBusy(true); setErr(null);
    try { await api.updateSourceSpec(ws.name, name, specBody(shape, rows)); await onSaved(); }
    catch (e) { setErr(e); setBusy(false); }
  }

  return (
    <section aria-label={`spec for ${name}`} style={{ marginTop: 10 }}>
      <b>Update the spec</b>
      <p className="note" style={{ margin: "4px 0 8px" }}>Say what this file means now, or upload a corrected
        file instead. I judge the drift again as soon as you save.</p>
      <div className="scroll"><table>
        <thead><tr><th>keep</th><th>column</th><th>type</th><th>key</th></tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.name} style={{ opacity: r.keep ? 1 : 0.5 }}>
              <td><input aria-label={`keep ${r.name}`} checked={r.keep} type="checkbox"
                         onChange={(e) => set(i, { keep: e.target.checked, key: e.target.checked && r.key })} /></td>
              <td className="mono">{r.name}</td>
              <td><select aria-label={`type ${r.name}`} disabled={!r.keep} value={r.type}
                          onChange={(e) => set(i, { type: e.target.value })}>
                {[...new Set([...TYPES, r.type])].map((t) => <option key={t} value={t}>{t}</option>)}
              </select></td>
              <td><input aria-label={`key ${r.name}`} checked={r.key} disabled={!r.keep} type="checkbox"
                         onChange={(e) => set(i, { key: e.target.checked })} /></td>
            </tr>
          ))}
        </tbody>
      </table></div>
      {dropped.length > 0 && (
        <p className="note" style={{ margin: "8px 0 0" }}>Your silver models do not change here. One that selects{" "}
          {dropped.map((r) => r.name).join(", ")} reads null for rows from this version on.</p>
      )}
      <div className="row">
        <button className="btn" disabled={busy || !rows.some((r) => r.keep)} onClick={save} type="button">
          Update the spec
        </button>
      </div>
      <Err e={err} />
    </section>
  );
}

function SourceCard({ name, ws, role, versions, report, onChanged }) {
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const latest = versions.at(-1);

  async function send() {
    setBusy(true); setErr(null);
    try {
      const made = await api.upload([file]);
      await api.addSourceVersion(ws.name, name, `${made.id}/${made.files[0].name}`);
      setFile(null);
      await onChanged({ newVersion: true });
    } catch (e) { setErr(e); }
    setBusy(false);
  }

  return (
    <section aria-label={`source ${name}`}>
      <Panel>
        <div className="row" style={{ margin: 0 }}>
          <b className="mono">{name}</b>
          <span className="badge hold">{latest.source.format}</span>
          <span className="note">version {latest.version}</span>
        </div>
        <div className="scroll" style={{ marginTop: 10 }}><table>
          <thead><tr><th>version</th><th>file</th><th>uploaded</th><th>rows loaded</th></tr></thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.version}>
                <td className="mono">v{v.version}</td>
                <td className="mono">{v.upload.split("/").at(-1)}</td>
                <td className="note">{new Date(v.created_at * 1000).toLocaleString()}</td>
                <td className="mono">{v.runs.at(-1)?.rows ?? "not loaded yet"}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
        {report && <DriftReport name={name} report={report} />}
        {report?.blocking && canAdmin(role) && (
          <SpecEditor key={`${latest.version}:${report.items.length}`} name={name} onSaved={() => onChanged({})}
                      report={report} shape={latest.source} ws={ws} />
        )}
        {report?.blocking && !canAdmin(role) && (
          <p className="note">Changing the spec needs an admin. A corrected file also clears this.</p>
        )}
        {canWrite(role) && (
          <div className="row" style={{ marginTop: 12 }}>
            <span>
              <label className="note" htmlFor={`version-${name}`}>New version of {name}</label><br />
              <input accept={ACCEPT} id={`version-${name}`} key={latest.version}
                     onChange={(e) => setFile(e.target.files[0] ?? null)} type="file" />
            </span>
            <button className="btn" disabled={busy || !file} onClick={send} style={{ alignSelf: "flex-end" }} type="button">
              {busy ? "Uploading…" : "Upload new version"}
            </button>
          </div>
        )}
        <Err e={err} />
      </Panel>
    </section>
  );
}

function RunPanel({ ws, blocked, jobId, setJobId, onFinished }) {
  const { job, log, err: pollErr } = useJob(jobId);
  const [mode, setMode] = useState("append");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const done = isDone(job);

  useEffect(() => { if (done) onFinished(); }, [done, onFinished]);

  async function start() {
    setBusy(true); setErr(null); setJobId(null);
    try { setJobId((await api.createJob("run", ws.name, { mode })).id); } catch (e) { setErr(e); }
    setBusy(false);
  }

  return (
    <section aria-label="refresh run">
      <Panel>
        <div className="row" style={{ margin: 0 }}>
          <span>
            <label className="note" htmlFor="run-mode">how to load</label><br />
            <select id="run-mode" onChange={(e) => setMode(e.target.value)} value={mode}>
              <option value="append">append the latest versions</option>
              <option value="full">rebuild from every version</option>
            </select>
          </span>
          <button className="btn" disabled={busy || blocked || (jobId && !done)} onClick={start}
                  style={{ alignSelf: "flex-end" }} type="button">Run the pipeline</button>
        </div>
        {blocked && <p className="note" style={{ margin: "8px 0 0" }}>Blocking drift keeps the last good data
          in place, and Ask keeps serving it. Update the spec or upload a corrected file to run.</p>}
        {jobId && (
          <div style={{ marginTop: 10 }}>
            <JobState job={job} />
            {job?.error && <Err e={job.error} />}
            <JobLog text={log} />
            {(job?.result?.layers ?? []).map((l) => (
              <p key={l.layer} style={{ margin: "6px 0 0" }}>
                <span className={`badge ${l.passed ? "ok" : "bad"}`}>{l.layer}</span>{" "}
                <span className="note">{l.headline}</span>
              </p>
            ))}
            {job?.state === "succeeded" && !runPassed(job) && (
              <p className="note">The run finished and a check failed.</p>
            )}
          </div>
        )}
        <Err e={err || pollErr} />
      </Panel>
    </section>
  );
}

// Bronze holds the file as received, so the typed names live in silver. Same shape the wizard makes.
export function silverFor(source) {
  const select = source.columns.map((c) => (
    c.type === "string" || c.type === "varchar"
      ? `"${c.name}" as "${c.name}"`
      : `try_cast("${c.name}" as ${c.type}) as "${c.name}"`
  ));
  return `select ${select.join(",\n       ")}\nfrom bronze_${source.name}\n`;
}

function AddDataset({ ws, onAdded }) {
  const [files, setFiles] = useState([]);
  const [uploadId, setUploadId] = useState(null);
  const [jobId, setJobId] = useState(null);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const { job, log, err: pollErr } = useJob(jobId);

  useEffect(() => {
    if (job?.state !== "succeeded" || !uploadId) return;
    api.uploadProposal(uploadId).then((p) => setDraft(p.workspace.sources.map((s) => ({ ...s, was: s.name })))).catch(setErr);
  }, [job?.state, uploadId]);

  async function send() {
    setBusy(true); setErr(null); setDraft(null);
    try {
      const made = await api.upload(files);
      setUploadId(made.id);
      setJobId((await api.startProfile(made.id)).id);
    } catch (e) { setErr(e); }
    setBusy(false);
  }

  async function add() {
    setBusy(true); setErr(null);
    try {
      const sources = draft.map(({ was: _was, ...s }) => s);
      const silver = Object.fromEntries(sources.map((s) => [`${s.name}.sql`, silverFor(s)]));
      await api.addSources(ws.name, { from_upload: uploadId, sources, models: { silver } });
      setDraft(null); setJobId(null); setUploadId(null); setFiles([]);
      await onAdded();
    } catch (e) { setErr(e); }
    setBusy(false);
  }

  const badName = draft?.some((s) => !IDENT.test(s.name));
  return (
    <section aria-label="add another dataset">
      <h3>Add another dataset</h3>
      <p className="note">New files join this workspace as new sources, and what it already reads stays as
        it is. Your accepted ontology does not read them yet, so Ask cannot reach them.</p>
      <Panel>
        <label className="note" htmlFor="dataset-files">Choose files for a new dataset</label><br />
        <input accept={ACCEPT} id="dataset-files" multiple onChange={(e) => setFiles([...e.target.files])}
               style={{ margin: "6px 0 10px" }} type="file" />
        <div>
          <button className="btn" disabled={busy || files.length === 0} onClick={send} type="button">
            {busy && !draft ? "Uploading…" : "Upload and profile"}
          </button>
        </div>
        {jobId && !draft && (
          <div style={{ marginTop: 10 }}>
            <JobState job={job} />
            {job?.error && <Err e={job.error} />}
            <JobLog text={log} />
          </div>
        )}
        {draft && draft.map((s, i) => (
          <div key={s.was} style={{ marginTop: 12 }}>
            <label className="note">source name{" "}
              <input aria-label={`name of the new source ${s.was}`} type="text" value={s.name}
                     onChange={(e) => setDraft(draft.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
            </label>
            <span className="note"> from {s.upload.split("/").at(-1)}</span>
            <div className="scroll" style={{ marginTop: 6 }}><table>
              <thead><tr><th>column</th><th>type</th><th>key</th></tr></thead>
              <tbody>{s.columns.map((c) => (
                <tr key={c.name}><td className="mono">{c.name}</td><td className="mono">{c.type}</td><td>{c.key ? "key" : ""}</td></tr>
              ))}</tbody>
            </table></div>
          </div>
        ))}
        {draft?.length === 0 && <p className="note">I could not read a source out of these files.</p>}
        {draft?.length > 0 && (
          <div className="row">
            {badName && <span className="err">Use lowercase letters, digits and underscores for a source name.</span>}
            <button className="btn" disabled={busy || badName} onClick={add} type="button">Add to this workspace</button>
          </div>
        )}
        <Err e={err || pollErr} />
      </Panel>
    </section>
  );
}

export default function TenantSources({ ws }) {
  const [me, setMe] = useState(null);
  const [overview, setOverview] = useState(null);
  const [versions, setVersions] = useState({});
  const [jobId, setJobId] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try {
      const now = await api.drift(ws.name);
      const names = Object.keys(now.sources);
      const lists = await Promise.all(names.map((n) => api.sourceVersions(ws.name, n)));
      setVersions(Object.fromEntries(names.map((n, i) => [n, lists[i].versions])));
      setOverview(now);
      setErr(null);
    } catch (e) { setErr(e); }
  }, [ws.name]);

  useEffect(() => {
    load();
    api.me().then(setMe).catch(setErr);
    // A run still going when you came back to this screen.
    api.jobs(ws.name).then((jobs) => {
      const going = jobs.find((j) => j.kind === "run" && !isDone(j));
      if (going) setJobId(going.id);
    }).catch(setErr);
  }, [ws.name, load]);

  const changed = useCallback(async ({ newVersion } = {}) => {
    if (newVersion) setJobId(null); // that run was about the file before this one
    await load();
  }, [load]);

  const names = overview ? Object.keys(overview.sources) : [];
  const blocked = names.some((n) => overview.sources[n].blocking);
  const role = me?.role;
  return (
    <>
      <h2>Your data sources</h2>
      <p className="sub">Each file you gave me, and every version of it. When the next export arrives,
        upload it here. I tell you what changed, and a change that would break the run stops the run
        until you decide. Older versions stay listed.</p>
      <Err e={err} />
      {!overview && !err && <p className="note">Loading your sources.</p>}
      {me && names.map((n) => (
        <SourceCard key={n} name={n} onChanged={changed} report={overview.sources[n]} role={role} versions={versions[n] ?? []} ws={ws} />
      ))}
      {me && overview && canWrite(role) && (
        <RunPanel blocked={blocked} jobId={jobId} onFinished={load} setJobId={setJobId} ws={ws} />
      )}
      {me && overview && canAdmin(role) && <AddDataset onAdded={() => changed({})} ws={ws} />}
    </>
  );
}
