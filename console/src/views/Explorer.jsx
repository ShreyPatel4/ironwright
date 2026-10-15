import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err, Rows } from "../components.jsx";

export default function Explorer({ ws }) {
  const [tables, setTables] = useState([]);
  const [sql, setSql] = useState("");
  const [out, setOut] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

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
        </div>
      </div>
    </>
  );
}
