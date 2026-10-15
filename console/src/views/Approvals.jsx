import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

export default function Approvals({ ws }) {
  const [pending, setPending] = useState([]);
  const [approver, setApprover] = useState("role:category_manager");
  const [last, setLast] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(() => {
    api.approvals(ws.name).then(setPending).catch(setErr);
  }, [ws.name]);
  useEffect(() => { setLast(null); setErr(null); load(); }, [load]);

  async function decide(seq, decision) {
    setErr(null);
    try {
      setLast(await api.decide(ws.name, seq, { approved_by: approver, decision }));
      load();
    } catch (e) { setErr(e); }
  }

  return (
    <>
      <h2>Approvals</h2>
      <p className="sub">Actions the policy parked: anything marked requires_approval waits here
        until a human decides. Approving re-runs the action through the full policy path with
        your identity on the audit row; denying is itself an audit row.</p>
      <div className="row">
        <label className="note" htmlFor="approver">decide as</label>
        <input id="approver" onChange={(e) => setApprover(e.target.value)}
               placeholder="the approving role, e.g. role:category_manager" type="text" value={approver} />
        <button className="btn ghost" onClick={load}>Refresh</button>
      </div>
      <Err e={err} />
      {last && <p aria-live="polite" className="note">last decision: seq {last.seq} → <b>{last.status}</b></p>}
      {pending.length === 0 ? <p className="note">Inbox empty. Nothing is waiting on a human.</p> : (
        <div className="scroll"><table>
          <thead><tr><th>seq</th><th>requested by</th><th>action</th><th>params</th><th></th></tr></thead>
          <tbody>
            {pending.map((p) => (
              <tr key={p.seq}>
                <td className="mono">{p.seq}</td>
                <td className="mono">{p.actor}</td>
                <td className="mono">{p.action}</td>
                <td className="mono">{JSON.stringify(p.params)}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <button className="btn" onClick={() => decide(p.seq, "approve")}>Approve</button>{" "}
                  <button className="btn danger" onClick={() => decide(p.seq, "deny")}>Deny</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </>
  );
}
