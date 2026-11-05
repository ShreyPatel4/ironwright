import { useEffect, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";
import { canWrite, JobLog, JobState, Loading, Panel, useJob } from "./shared.jsx";

const percent = (rate) => `${Math.round(rate * 100)}%`;

function FileProfile({ file }) {
  if (file.error) {
    return <Panel><b className="mono">{file.name}</b><Err e={file.error} /></Panel>;
  }
  return (
    <Panel>
      <b className="mono">{file.name}</b>
      <span className="note"> {file.rows} rows, {file.format}. {file.format_reason}</span>
      <div className="scroll" style={{ marginTop: 10 }}>
        <table>
          <thead><tr><th>column</th><th>type</th><th>nulls</th><th>why this type</th></tr></thead>
          <tbody>
            {file.columns.map((c) => (
              <tr key={c.name}>
                <td className="mono">{c.name}</td>
                <td className="mono">{c.type}</td>
                <td className="mono">{percent(c.null_rate)}</td>
                <td>{c.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h3>Candidate keys</h3>
      {file.candidate_keys.length === 0 && <p className="note">No candidate key in this file.</p>}
      {file.candidate_keys.map((k) => (
        <p key={k.column} style={{ margin: "4px 0" }}>
          <span className="mono">{k.column}</span> <span className="note">{k.reason}</span>
        </p>
      ))}
    </Panel>
  );
}

export default function ProfileStep({ journey, advance }) {
  const { upload, profileJob, me } = journey;
  const [startedId, setStartedId] = useState(null);
  const jobId = startedId ?? profileJob?.id ?? null;
  const { job, log, err: pollErr } = useJob(jobId);
  const [profile, setProfile] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const state = job?.state;

  useEffect(() => {
    if (state !== "succeeded") return;
    api.uploadProfile(upload.id).then(setProfile).catch(setErr);
  }, [state, upload?.id]);

  async function start() {
    setBusy(true); setErr(null);
    try { setStartedId((await api.createJob("profile", upload.id, { upload_id: upload.id })).id); }
    catch (e) { setErr(e); }
    setBusy(false);
  }

  return (
    <>
      <h2>Profile</h2>
      <p className="sub">I read each file and guess its types, nulls, keys and joins, by rules. Each
        guess says why, so you can disagree with it on the next step.</p>
      {!upload && <p className="note">There are no files to profile yet.</p>}
      {upload && !jobId && !canWrite(me.role) && <p className="note">Nobody has profiled these files yet.</p>}
      {upload && !jobId && canWrite(me.role) && (
        <button className="btn" disabled={busy} onClick={start} type="button">Profile these files</button>
      )}
      {jobId && state !== "succeeded" && (
        <Panel>
          <JobState job={job} />
          {job?.error && <Err e={job.error} />}
          <JobLog text={log} />
          {(state === "failed" || state === "cancelled") && canWrite(me.role) && (
            <button className="btn" disabled={busy} onClick={start} style={{ marginTop: 10 }} type="button">
              Profile again
            </button>
          )}
        </Panel>
      )}
      <Err e={err || pollErr} />
      {state === "succeeded" && !profile && !err && <Loading what="the profile" />}
      {profile && (
        <>
          {profile.files.map((f) => <FileProfile file={f} key={f.name} />)}
          {profile.skipped.length > 0 && (
            <p className="note">One profile reads a limited number of files. I left out: {profile.skipped.join(", ")}.</p>
          )}
          <h3>Candidate joins</h3>
          <Panel>
            {profile.candidate_joins.length === 0 && (
              <p className="note" style={{ margin: 0 }}>No join candidates between these files.</p>
            )}
            {profile.candidate_joins.map((j) => (
              <p key={`${j.left}|${j.right}`} style={{ margin: "4px 0" }}>
                <span className="mono">{j.left}</span> to <span className="mono">{j.right}</span>
                <br /><span className="note">{j.reason}</span>
              </p>
            ))}
          </Panel>
          <button className="btn" onClick={() => advance()} type="button">Continue to Map</button>
        </>
      )}
    </>
  );
}
