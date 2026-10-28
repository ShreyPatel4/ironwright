import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

export default function Approvals({ ws }) {
  const [pending, setPending] = useState([]);
  const [escs, setEscs] = useState([]);
  const [approver, setApprover] = useState("");
  const [expires, setExpires] = useState("");
  const [last, setLast] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(() => {
    api.approvals(ws.name).then(setPending).catch(setErr);
    api.escalations(ws.name).then(setEscs).catch(() => setEscs([]));
  }, [ws.name]);
  useEffect(() => { setLast(null); setErr(null); load(); }, [load]);

  async function decide(seq, decision) {
    setErr(null);
    try {
      setLast(await api.decide(ws.name, seq, { approved_by: approver, decision }));
      load();
    } catch (e) { setErr(e); }
  }

  async function decideEsc(id, decision) {
    setErr(null);
    try {
      const out = await api.decideEscalation(ws.name, id, {
        decision, decided_by: approver,
        expires: decision === "grant" ? (expires || null) : null,
      });
      setLast({ seq: id, status: out.status });
      load();
    } catch (e) { setErr(e); }
  }

  const noIdentity = !approver.trim();

  return (
    <>
      <h2>Approvals</h2>
      <p className="sub">Actions the policy parked: anything marked requires_approval waits here
        until a human decides. Approving re-runs the action through the full policy path with
        your identity on the audit row; denying is itself an audit row.</p>
      <div className="row">
        <label className="note" htmlFor="approver">decide as</label>
        <input id="approver" onChange={(e) => setApprover(e.target.value)}
               placeholder="the approving role, e.g. role:…" style={{ minWidth: 220 }}
               type="text" value={approver} />
        <button className="btn ghost" onClick={load}>Refresh</button>
        {noIdentity && <span className="note">name the deciding role to enable the buttons</span>}
      </div>
      <Err e={err} />
      {last && <p aria-live="polite" className="note">last decision: {last.seq} → <b>{last.status}</b></p>}
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
                  <button className="btn" disabled={noIdentity}
                          onClick={() => decide(p.seq, "approve")}>Approve</button>{" "}
                  <button className="btn danger" disabled={noIdentity}
                          onClick={() => decide(p.seq, "deny")}>Deny</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}

      <h3>Escalations</h3>
      <p className="note">A denial raised to the owning role. A grant becomes a narrow policy
        rule for that principal, that action, that object, consulted by every serving decision
        until it dies at the start of the expiry date, UTC. A hand-written forbid still wins.</p>
      <div className="row">
        <label className="note" htmlFor="esc-exp">grant expires (ISO date)</label>
        <input id="esc-exp" onChange={(e) => setExpires(e.target.value)}
               placeholder="2026-11-01" style={{ width: 130 }} type="text" value={expires} />
      </div>
      {escs.length === 0 ? <p className="note">No pending escalations. A denied action's page
        under SEMANTIC, Ontology offers "escalate" beside the denied chip.</p> : (
        <div className="scroll"><table>
          <thead><tr><th>id</th><th>principal</th><th>wants</th><th>on</th><th>denied by</th><th>owner</th><th></th></tr></thead>
          <tbody>{escs.map((e) => (
            <tr key={e.id}>
              <td className="mono">{e.id}</td><td className="mono">{e.principal}</td>
              <td className="mono">{e.action}</td><td className="mono">{e.object_id}</td>
              <td className="mono">{e.denying_rule}</td><td className="mono">{e.owner_role}</td>
              <td style={{ whiteSpace: "nowrap" }}>
                <button className="btn" disabled={noIdentity || !expires}
                        onClick={() => decideEsc(e.id, "grant")}
                        title={expires ? "" : "set an expiry first"}>Grant</button>{" "}
                <button className="btn danger" disabled={noIdentity}
                        onClick={() => decideEsc(e.id, "deny")}>Deny</button>
              </td>
            </tr>))}
          </tbody>
        </table></div>
      )}
    </>
  );
}
