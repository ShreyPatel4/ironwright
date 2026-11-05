import { useCallback, useEffect, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";

const WORKING = ["queued", "running"];

export default function OrgData({ slug, onDeleted }) {
  const [exports, setExports] = useState(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(() => api.exports(slug).then(setExports).catch(setErr), [slug]);
  useEffect(() => { load(); }, [load]);

  const latest = exports?.[0] ?? null;
  const working = latest && WORKING.includes(latest.state);
  const ready = latest?.state === "succeeded" ? latest : (exports?.find((j) => j.state === "succeeded") ?? null);
  const totals = ready?.result?.totals;
  // The job runs on a worker, so this screen looks again until it ends.
  useEffect(() => {
    if (!working) return undefined;
    const t = setInterval(load, 1000);
    return () => clearInterval(t);
  }, [working, load]);

  async function start() {
    setErr(null);
    try { await api.createExport(slug); await load(); }
    catch (e) { setErr(e); }
  }

  async function download() {
    setErr(null);
    try { await api.downloadExport(slug, latest.id); }
    catch (e) { setErr(e); }
  }

  async function destroy() {
    setBusy(true); setErr(null);
    try { await api.deleteOrg(slug, typed); onDeleted(); }
    catch (e) { setErr(e); setBusy(false); }
  }

  return (
    <>
      <h3>Export</h3>
      <p className="sub">One zip with your workspace specs, your accepted ontologies and your silver and
        gold tables as Parquet. Your raw uploads are not in it. A new export replaces the last one.</p>
      <div className="row">
        <button className="btn" disabled={working} onClick={start}>Export my data</button>
        {working && <span className="badge hold">preparing, {latest.state}</span>}
        {latest?.state === "succeeded" && (
          <button className="btn ghost" onClick={download}>
            Download {totals?.tables ?? 0} tables, {totals?.rows ?? 0} rows
          </button>
        )}
        {latest?.state === "failed" && <span className="badge bad">failed: {latest.error}</span>}
      </div>

      <h3 style={{ marginTop: 28 }}>Delete this org</h3>
      <p className="sub">This removes every upload, workspace, lake table, job and record of the org, and every
        account that belongs to no other org. Nobody can sign in to it from the moment you confirm.
        It cannot be undone. One line stays in the audit log to say it existed. It holds no names.</p>
      {exports && !ready && !working && (
        <div className="err" data-testid="export-empty">You have not exported this org. Once you delete it,
          your tables are gone. Export first if you want to keep them.</div>
      )}
      {ready && !working && !totals?.tables && (
        <div className="err" data-testid="export-empty">Your latest export holds no tables, so it keeps none of
          your data. Run your workspaces and export again if you want to keep them.</div>
      )}
      <div className="row">
        <label className="note" htmlFor="confirm-slug">Type the org slug to confirm: <span className="mono">{slug}</span></label>
        <input autoComplete="off" id="confirm-slug" onChange={(e) => setTyped(e.target.value)}
               style={{ width: 240 }} type="text" value={typed} />
        <button className="btn danger" disabled={busy || typed !== slug} onClick={destroy}>Delete this org</button>
      </div>
      <Err e={err} />
    </>
  );
}
