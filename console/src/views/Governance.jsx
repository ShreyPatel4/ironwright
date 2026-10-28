import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

export default function Governance({ ws }) {
  const [gov, setGov] = useState(null);
  const [principal, setPrincipal] = useState("");
  const [action, setAction] = useState("");
  const [verdict, setVerdict] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    setGov(null); setVerdict(null); setErr(null);
    api.governance(ws.name).then((g) => {
      setGov(g);
      const first = Object.keys(g.actions)[0];
      if (first) setAction(`action:${first}`);
    }).catch(setErr);
  }, [ws.name]);

  async function check() {
    setErr(null);
    try {
      setVerdict(await api.governanceCheck(ws.name, { principal, action, env: {} }));
    } catch (e) { setErr(e); }
  }

  return (
    <>
      <h2>Governance</h2>
      <p className="sub">{gov?.note ?? "Default deny: an action either has a permit or it does not run."}</p>
      <Err e={err} />

      <h3>Would this be allowed? (dry run against the real policy engine)</h3>
      <div className="row">
        <label className="note" htmlFor="gov-principal">as principal</label>
        <input id="gov-principal" onChange={(e) => setPrincipal(e.target.value)}
               placeholder="agent:…, user:…, role:…" type="text" value={principal} />
        <label className="note" htmlFor="gov-action">may they</label>
        <input id="gov-action" onChange={(e) => setAction(e.target.value)}
               placeholder="action:… or function:…" type="text" value={action} />
        <button className="btn" disabled={!principal.trim() || !action.trim()}
                onClick={check}>Check</button>
        {verdict && (
          <span className={`badge ${verdict.decision.allowed ? "ok" : "bad"}`}>
            {verdict.decision.allowed ? "permit" : "deny"} · {verdict.decision.reason}
          </span>
        )}
      </div>

      {gov && (
        <>
          <h3>Actions and their approval chains</h3>
          <div className="scroll"><table>
            <thead><tr><th>action</th><th>approval</th><th>approver roles</th><th>what it does</th></tr></thead>
            <tbody>
              {Object.entries(gov.actions).map(([a, d]) => (
                <tr key={a}>
                  <td className="mono">{a}</td>
                  <td>{d.requires_approval
                    ? <span className="badge hold">human approval required</span>
                    : <span className="badge ok">policy only</span>}</td>
                  <td className="mono">{d.approver_roles.join(", ") || ""}</td>
                  <td>{d.description}</td>
                </tr>
              ))}
            </tbody>
          </table></div>

          <h3>Policy rules, in evaluation order</h3>
          <div className="scroll"><table>
            <thead><tr><th>id</th><th>effect</th><th>principal</th><th>action</th><th>when</th></tr></thead>
            <tbody>
              {gov.rules.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.id}</td>
                  <td><span className={`badge ${r.effect === "permit" ? "ok" : "bad"}`}>{r.effect}</span></td>
                  <td className="mono">{r.principal}</td>
                  <td className="mono">{r.action}</td>
                  <td className="mono">{r.when ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table></div>

          {(gov.generated_rules ?? []).length > 0 && (
            <>
              <h3>Generated rules and grants in force</h3>
              <p className="note">Applied through the reviewed-diff path and consulted by every
                serving decision; an expired grant drops out on its own. Hand-written rules above
                win a conflict.</p>
              <div className="scroll"><table>
                <thead><tr><th>id</th><th>effect</th><th>principal</th><th>action</th><th>when</th><th>expires</th></tr></thead>
                <tbody>
                  {gov.generated_rules.map((r) => (
                    <tr key={r.id}>
                      <td className="mono">{r.id}</td>
                      <td><span className={`badge ${r.effect === "permit" ? "ok" : "bad"}`}>{r.effect}</span></td>
                      <td className="mono">{r.principal}</td>
                      <td className="mono">{r.action}</td>
                      <td className="mono">{r.when ?? ""}</td>
                      <td className="mono">{r.expires ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            </>
          )}
        </>
      )}
    </>
  );
}
