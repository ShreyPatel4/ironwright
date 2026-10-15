import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

export default function Loads({ ws }) {
  const [loads, setLoads] = useState(null);
  const [openChecks, setOpenChecks] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    setLoads(null); setOpenChecks(null); setErr(null);
    api.loads(ws.name).then(setLoads).catch(setErr);
  }, [ws.name]);

  return (
    <>
      <h2>Loads</h2>
      <p className="sub">Write-audit-publish, shown as it ran. Every write lands on an audit
        branch, runs its checks there, and only fast-forwards to main when they pass. Publish
        to main is not a button here because it is not a human decision: the checks decide,
        and failures land in quarantine instead.</p>
      <Err e={err} />
      {loads && (
        <>
          <div className="row">
            <span className="mono note">run {loads.run_id}</span>
            <span className={`badge ${loads.quarantine_tables.length ? "hold" : "ok"}`}>
              {loads.quarantine_tables.length
                ? `${loads.quarantine_tables.length} quarantine tables`
                : "nothing quarantined"}
            </span>
          </div>
          <div className="scroll"><table>
            <thead><tr><th>table</th><th>rows</th><th>audit branch</th><th>checks</th><th>outcome</th></tr></thead>
            <tbody>
              {loads.writes.map((w) => {
                const failed = w.checks.filter((c) => !c.passed).length;
                return (
                  <tr key={w.table + w.branch}>
                    <td className="mono">{w.table}</td>
                    <td className="mono">{w.rows}</td>
                    <td className="mono">{w.branch}</td>
                    <td>
                      <button className="btn ghost"
                              onClick={() => setOpenChecks(openChecks === w.branch ? null : w.branch)}>
                        {w.checks.length} checks{failed ? `, ${failed} failed` : ""}
                      </button>
                    </td>
                    <td><span className={`badge ${w.published ? "ok" : "bad"}`}>
                      {w.published ? "published to main" : "quarantined"}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table></div>
          {openChecks && (
            <>
              <h3>Checks on {openChecks}</h3>
              <div className="scroll"><table>
                <thead><tr><th>check</th><th>kind</th><th>severity</th><th>owner</th><th>result</th></tr></thead>
                <tbody>
                  {loads.writes.find((w) => w.branch === openChecks)?.checks.map((c, i) => (
                    <tr key={i}>
                      <td className="mono">{c.check}</td><td>{c.kind}</td><td>{c.severity}</td>
                      <td className="mono">{c.owner}</td>
                      <td><span className={`badge ${c.passed ? "ok" : "bad"}`}>
                        {c.passed ? "pass" : `fail (${c.failing_rows ?? "?"} rows)`}</span></td>
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
