import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

/* BI embed: the honest slice of "Looker, Power BI, Metabase over the lake".
   The mirror button copies every silver and gold table into postgres and
   verifies each copy row for row; the badges show that verdict, per table.
   The URL input saves one http(s) link the frame renders. We claim the
   mirror and the frame; the dashboard inside is the user's own BI tool.

   API names the integrator wires in api.js:
     bi:       (ws)          GET  /workspaces/{ws}/bi
     setBi:    (ws, url)     PUT  /workspaces/{ws}/bi        {url}
     biMirror: (ws, schema?) POST /workspaces/{ws}/bi/mirror {schema?} */

function MatchBadge({ t }) {
  return (
    <span className="mono" style={{
      background: t.match ? "var(--ok-bg, rgba(0,128,0,.08))" : "var(--err-bg, rgba(200,0,0,.08))",
      border: "1px solid var(--rule)", borderRadius: "var(--rad)",
      color: t.match ? "var(--ok, green)" : "var(--err, #b00020)",
      display: "inline-block", fontSize: 12, padding: "2px 8px",
    }} title={`${t.rows} rows, read back and compared after canonical sort`}>
      {t.table} · {t.rows} rows · {t.match ? "match" : "MISMATCH"}
    </span>
  );
}

export default function BIEmbed({ ws }) {
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState("");
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(() => {
    api.bi(ws.name).then((c) => { setConfig(c); setDraft(c.url || ""); }).catch(setErr);
  }, [ws.name]);
  useEffect(() => { setReport(null); setErr(null); load(); }, [load]);

  async function mirror() {
    setBusy(true); setErr(null);
    try { setReport(await api.biMirror(ws.name)); } catch (e) { setErr(e); }
    setBusy(false);
  }

  async function save() {
    setErr(null);
    try { setConfig(await api.setBi(ws.name, draft.trim())); } catch (e) { setErr(e); }
  }

  return (
    <>
      <h2>BI embed</h2>
      <p className="sub">Point your own BI tool (Metabase, Looker, Power BI) at this workspace.
        The mirror copies every silver and gold table into a postgres schema, then reads each
        copy back and verifies it row for row; the badges below are that verdict, not a hope.
        deploy/bi in the repo has a Metabase recipe.</p>
      <Err e={err} />

      <div className="row">
        <button className="btn" disabled={busy} onClick={mirror}>
          {busy ? "Mirroring…" : "Mirror lake to postgres"}</button>
        {report && <span className="note">
          schema <span className="mono">{report.schema}</span>
          {report.matched ? " · every table verified" : " · at least one table did not verify"}
        </span>}
      </div>
      {report && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, margin: "10px 0 18px" }}>
          {report.tables.map((t) => <MatchBadge key={t.table} t={t} />)}
        </div>
      )}

      <div className="row">
        <label className="note" htmlFor="bi-url">dashboard URL</label>
        <input id="bi-url" onChange={(e) => setDraft(e.target.value)}
               placeholder="https://your-bi-host/dashboard/…" style={{ flex: 1, minWidth: 320 }}
               type="url" value={draft} />
        <button className="btn" disabled={!draft.trim() || draft.trim() === config?.url}
                onClick={save}>Save</button>
      </div>
      <p className="note">Only an absolute http(s) URL is accepted; anything else is rejected
        at save. Saved {config?.saved_at ? new Date(config.saved_at * 1000).toLocaleString() : "never"}.</p>

      {config?.url && (
        <>
          <p className="note">The dashboard below is your BI tool rendering the mirrored schema.
            Ironwright claims the mirror and this frame, nothing more.</p>
          <iframe sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                  src={config.url} style={{ background: "var(--bg-1)",
                    border: "1px solid var(--rule)", borderRadius: "var(--rad)",
                    height: "70vh", width: "100%" }}
                  title={`Embedded BI dashboard for ${ws.name}`} />
        </>
      )}
      {!config?.url && <p className="note">No dashboard URL saved yet for this workspace.</p>}
    </>
  );
}
