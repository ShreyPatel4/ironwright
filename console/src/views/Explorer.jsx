import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err, Rows } from "../components.jsx";

export default function Explorer({ ws }) {
  const [tables, setTables] = useState([]);
  const [sql, setSql] = useState("");
  const [out, setOut] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [engines, setEngines] = useState(null);
  const [engBusy, setEngBusy] = useState(false);

  async function checkEngines() {
    setEngBusy(true); setErr(null); setEngines(null);
    try { setEngines(await api.enginesCheck(ws.name)); } catch (e) { setErr(e); }
    setEngBusy(false);
  }

  useEffect(() => {
    setOut(null); setErr(null);
    api.lakeTables(ws.name).then((t) => {
      setTables(t);
      const gold = t.find((x) => x.layer === "gold");
      if (gold) setSql(`select * from ${gold.layer}_${gold.name} limit 20`);
    }).catch(setErr);
  }, [ws.name]);

  async function run() {
    setBusy(true); setErr(null);
    try { setOut(await api.lakeQuery(ws.name, sql)); } catch (e) { setErr(e); setOut(null); }
    setBusy(false);
  }

  return (
    <>
      <h2>Data explorer</h2>
      <p className="sub">Read-only SQL over the lakehouse. Views are named layer_table; bronze
        views show the latest run, bronze_*_all every run. The server refuses anything that is
        not a single SELECT.</p>
      <div className="split">
        <div className="list">
          {tables.map((t) => (
            <button key={t.ident} onClick={() => setSql(`select * from ${t.layer}_${t.name} limit 20`)}>
              <span className={`layerchip ${t.layer}`}>{t.layer}</span> {t.name}
              <span className="note"> · {t.rows.toLocaleString()} rows</span>
            </button>
          ))}
        </div>
        <div>
          <textarea aria-label="SQL query, a single SELECT" onChange={(e) => setSql(e.target.value)} spellCheck={false} value={sql} />
          <div className="row">
            <button className="btn" disabled={busy || !sql.trim()} onClick={run}>{busy ? "Running…" : "Run query"}</button>
            {out?.truncated_at && <span className="note">showing first {out.truncated_at} rows</span>}
          </div>
          <Err e={err} />
          {out && <Rows columns={out.columns} rows={out.rows} />}

          <h3>Engine swap</h3>
          <p className="note">Transforms are SQL. This runs every model of this workspace
            unchanged on a second engine (postgres, from POSTGRES_URI) and compares the
            results to DuckDB's, value for value. Trino and Spark are not claimed.</p>
          <div className="row">
            <button className="btn ghost" disabled={engBusy} onClick={checkEngines}>
              {engBusy ? "Comparing engines…" : "Run models on postgres and compare"}
            </button>
            {engines && (
              <span aria-live="polite" className={`badge ${engines.match ? "ok" : "bad"}`}>
                {engines.match ? "identical on both engines" : "DIVERGED"} ·{" "}
                {engines.models.length} models
              </span>
            )}
          </div>
          {engines && (
            <div className="scroll"><table>
              <thead><tr><th>model</th><th>rows</th><th>match</th></tr></thead>
              <tbody>{engines.models.map((m) => (
                <tr key={m.model}>
                  <td className="mono">{m.model}</td>
                  <td className="mono">{m.rows}</td>
                  <td><span className={`badge ${m.match ? "ok" : "bad"}`}>
                    {m.match ? "identical" : m.error ?? "diverged"}</span></td>
                </tr>))}
              </tbody>
            </table></div>
          )}
        </div>
      </div>
    </>
  );
}
