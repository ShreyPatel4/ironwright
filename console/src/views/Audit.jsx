import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";
import { Loading } from "./onboarding/shared.jsx";

export default function Audit({ ws }) {
  const [rows, setRows] = useState(null);
  const [verify, setVerify] = useState(null);
  const [err, setErr] = useState(null);

  const latest = useRef(0);

  // Only the newest read may speak. An older answer landing late would paint rows over its failure.
  const load = useCallback(() => {
    const mine = ++latest.current;
    setErr(null);
    api.audit(ws.name)
      .then((r) => { if (mine === latest.current) setRows(r); })
      .catch((e) => { if (mine === latest.current) { setRows(null); setErr(e); } });
  }, [ws.name]);
  useEffect(() => { setVerify(null); setErr(null); load(); }, [load]);

  async function check() {
    setErr(null);
    try { setVerify(await api.auditVerify(ws.name)); } catch (e) { setErr(e); }
  }

  return (
    <>
      <h2>Audit</h2>
      <p className="sub">Every action in one hash-chained log: each row's hash covers the previous
        row's, so history cannot be edited quietly. Verify recomputes the whole chain server-side.</p>
      <div aria-live="polite" className="row">
        <button className="btn" onClick={check}>Verify the chain</button>
        <button className="btn ghost" onClick={load}>Refresh</button>
        {verify && (
          <span className={`badge ${verify.intact ? "ok" : "bad"}`}>
            {verify.intact ? `intact · ${verify.rows} rows` : "CHAIN BROKEN"}
          </span>
        )}
      </div>
      <Err e={err} />
      {rows === null && !err && <Loading what="the audit log" />}
      {rows?.length === 0 && <p className="note">No actions are recorded yet.</p>}
      {rows?.length > 0 && <div className="scroll"><table>
        <thead><tr><th>seq</th><th>actor</th><th>action</th><th>status</th><th>approved by</th><th>hash</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.seq}>
              <td className="mono">{r.seq}</td>
              <td className="mono">{r.actor}{r.on_behalf_of ? ` (for ${r.on_behalf_of})` : ""}</td>
              <td className="mono">{r.action}</td>
              <td><span className={`badge ${r.status === "needs_approval" ? "hold" : r.status === "denied" ? "bad" : "ok"}`}>{r.status}</span></td>
              <td className="mono">{r.approved_by ?? ""}</td>
              <td className="mono">{String(r.hash).slice(0, 10)}</td>
            </tr>
          ))}
        </tbody>
      </table></div>}
    </>
  );
}
