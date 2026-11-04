import { useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";
import { canWrite, Panel } from "./shared.jsx";

const ACCEPT = ".csv,.txt,.dat,.tsv";

export default function UploadStep({ journey, advance }) {
  const [files, setFiles] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const { uploads, workspace, me } = journey;

  async function send() {
    setBusy(true); setErr(null);
    try {
      const made = await api.upload(files);
      await api.createJob("profile", made.id, { upload_id: made.id });
      await advance("profile");
    } catch (e) { setErr(e); setBusy(false); }
  }

  return (
    <>
      <h2>Upload your files</h2>
      <p className="sub">Exports from your old systems: CSV, TSV, TXT or fixed width DAT. I store the
        bytes as they are and read them only for your org.</p>
      {canWrite(me.role) && !workspace && (
        <Panel>
          <label className="note" htmlFor="upload-files">Choose files</label><br />
          <input accept={ACCEPT} id="upload-files" multiple
                 onChange={(e) => setFiles([...e.target.files])}
                 style={{ margin: "6px 0 10px" }} type="file" />
          <div>
            <button className="btn" disabled={busy || files.length === 0} onClick={send} type="button">
              {busy ? "Uploading…" : "Upload and profile"}
            </button>
          </div>
          <Err e={err} />
        </Panel>
      )}
      {workspace && (
        <p className="note">Your workspace {workspace.name} already reads these files.</p>
      )}
      <h3>Your uploads</h3>
      {uploads.length === 0 && <p className="note">No files yet.</p>}
      {uploads.map((u, i) => (
        <Panel key={u.id}>
          <span className="note">{i === 0 ? "newest, and the one the next steps use" : "older"}</span>
          {u.files.map((f) => <div className="mono" key={f.name}>{f.name}</div>)}
        </Panel>
      ))}
    </>
  );
}
