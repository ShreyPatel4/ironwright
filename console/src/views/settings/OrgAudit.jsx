import { useCallback, useEffect, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";

/* The org's own log: people, keys and plans. Each workspace keeps a separate chain
   for its actions, on its own Audit screen. The filters narrow the list. The verdict
   always covers every row. */
export default function OrgAudit({ slug }) {
  const [got, setGot] = useState(null);
  const [emails, setEmails] = useState({});
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    setErr(null);
    try { setGot(await api.auditChain(slug, { actor, action })); }
    catch (e) { setErr(e); }
  }, [slug, actor, action]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    api.members(slug).then((m) => setEmails(
      Object.fromEntries(m.members.map((p) => [`user:${p.user_id}`, p.email])))).catch(setErr);
  }, [slug]);

  const name = (principal) => emails[principal] ?? principal;

  return (
    <>
      <p className="sub">Every change to this org's people, keys and plan, in one hash chain. Each row's
        hash covers the row before it, so an edit shows up here. This is the org's log. Each workspace's
        actions are on that workspace's own Audit screen.</p>
      <div aria-live="polite" className="row">
        <button className="btn" onClick={load}>Verify the chain</button>
        {got && (
          <span className={`badge ${got.verified ? "ok" : "bad"}`} data-testid="chain-status">
            {got.verified
              ? `intact · ${got.total} rows`
              : `CHAIN BROKEN at row ${got.broken_at}`}
          </span>
        )}
        {got?.unchained > 0 && (
          <span className="badge hold">{got.unchained} older rows have no hash and cannot be checked</span>
        )}
      </div>
      <div className="row">
        <select aria-label="Filter by actor" onChange={(e) => setActor(e.target.value)} value={actor}>
          <option value="">every actor</option>
          {got?.actors.map((a) => <option key={a} value={a}>{name(a)}</option>)}
        </select>
        <select aria-label="Filter by action" onChange={(e) => setAction(e.target.value)} value={action}>
          <option value="">every action</option>
          {got?.actions.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
      </div>
      <Err e={err} />
      <div className="scroll"><table>
        <thead><tr><th>seq</th><th>when</th><th>actor</th><th>action</th><th>detail</th><th>hash</th></tr></thead>
        <tbody>
          {got?.rows.map((r) => (
            <tr key={r.seq}>
              <td className="mono">{r.seq}</td>
              <td className="mono">{new Date(r.ts * 1000).toISOString().slice(0, 19)}</td>
              <td className="mono">{name(r.actor)}</td>
              <td className="mono">{r.action}</td>
              <td className="mono">{JSON.stringify(r.detail)}</td>
              <td className="mono">{r.hash ? r.hash.slice(0, 10) : "none"}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
    </>
  );
}
