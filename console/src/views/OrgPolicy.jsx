import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

/* Governance for a created organization: policy generated from the org
   chart, applied only through this reviewed diff, plus the escalation
   inbox whose grants are narrow expiring rules through the same path. */

export default function OrgPolicy({ ws }) {
  const [preview, setPreview] = useState(null);
  const [applied, setApplied] = useState(null);
  const [approver, setApprover] = useState("user:shreypatel");
  const [escs, setEscs] = useState([]);
  const [expires, setExpires] = useState("");
  const [err, setErr] = useState(null);

  const load = useCallback(() => {
    api.orgpolicyPreview(ws.name).then(setPreview).catch(setErr);
    api.escalations(ws.name).then(setEscs).catch(() => setEscs([]));
  }, [ws.name]);
  useEffect(() => { setPreview(null); setApplied(null); setErr(null); load(); }, [load]);

  async function apply() {
    setErr(null);
    try { setApplied(await api.orgpolicyApply(ws.name, approver)); load(); } catch (e) { setErr(e); }
  }

  async function decideEsc(id, decision) {
    setErr(null);
    try {
      await api.decideEscalation(ws.name, id, { decision, decided_by: approver,
                                                expires: decision === "grant" ? (expires || null) : null });
      load();
    } catch (e) { setErr(e); }
  }

  const d = preview?.diff;

  return (
    <>
      <h2>Governance, generated from the org</h2>
      <p className="sub">Who may do what is derived from who owns what: permits from data
        owners, approval requirements from the chains. Nothing changes until you apply the
        diff below, every application is logged, and hand-written rules always win over
        generated ones.</p>
      <Err e={err} />
      <div className="row">
        <label className="note" htmlFor="op-approver">acting as</label>
        <input id="op-approver" onChange={(e) => setApprover(e.target.value)}
               placeholder="who approves this application" type="text" value={approver} />
        <button className="btn" disabled={!preview} onClick={apply}>
          Apply {d ? `(${d.added.length} added, ${d.removed.length} removed)` : ""}
        </button>
        {applied && <span aria-live="polite" className="badge ok">applied · logged</span>}
      </div>
      {preview && (
        <>
          <h3>The diff under review</h3>
          {d.added.length + d.removed.length === 0 && (
            <p className="note">Generated policy matches what is applied; nothing to change.</p>
          )}
          <div className="scroll"><table>
            <thead><tr><th></th><th>rule</th><th>effect</th><th>principal</th><th>action</th><th>when</th></tr></thead>
            <tbody>
              {d.added.map((r) => (
                <tr key={`a${r.id}`}><td className="mono" style={{ color: "var(--success)" }}>+</td>
                  <td className="mono">{r.id}</td><td>{r.effect}</td>
                  <td className="mono">{r.principal}</td><td className="mono">{r.action}</td>
                  <td className="mono">{r.when ?? ""}</td></tr>
              ))}
              {d.removed.map((r) => (
                <tr key={`r${r.id}`}><td className="mono" style={{ color: "var(--danger)" }}>-</td>
                  <td className="mono">{r.id}</td><td>{r.effect}</td>
                  <td className="mono">{r.principal}</td><td className="mono">{r.action}</td>
                  <td className="mono">{r.when ?? ""}</td></tr>
              ))}
            </tbody>
          </table></div>
          <p className="note">{d.unchanged.length} rules unchanged · {preview.applied} currently applied.</p>
        </>
      )}

      <h3>Escalations</h3>
      <p className="note">A grant becomes a narrow policy rule for one principal, one action,
        one object, until the expiry you set. It lands through the same generated file and log.</p>
      <div className="row">
        <label className="note" htmlFor="op-exp">grant expires (ISO date)</label>
        <input id="op-exp" onChange={(e) => setExpires(e.target.value)}
               placeholder="2026-11-01" style={{ width: 130 }} type="text" value={expires} />
      </div>
      {escs.length === 0 ? <p className="note">No pending escalations.</p> : (
        <div className="scroll"><table>
          <thead><tr><th>id</th><th>principal</th><th>wants</th><th>on</th><th>denied by</th><th>owner</th><th></th></tr></thead>
          <tbody>{escs.map((e) => (
            <tr key={e.id}>
              <td className="mono">{e.id}</td><td className="mono">{e.principal}</td>
              <td className="mono">{e.action}</td><td className="mono">{e.object_id}</td>
              <td className="mono">{e.denying_rule}</td><td className="mono">{e.owner_role}</td>
              <td style={{ whiteSpace: "nowrap" }}>
                <button className="btn" disabled={!expires} onClick={() => decideEsc(e.id, "grant")}
                        title={expires ? "" : "set an expiry first"}>Grant</button>{" "}
                <button className="btn danger" onClick={() => decideEsc(e.id, "deny")}>Deny</button>
              </td>
            </tr>))}
          </tbody>
        </table></div>
      )}
    </>
  );
}
