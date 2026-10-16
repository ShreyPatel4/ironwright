import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

export default function Sources({ ws }) {
  const [sources, setSources] = useState([]);
  const [open, setOpen] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    setOpen(null); setErr(null);
    api.sources(ws.name).then(setSources).catch(setErr);
  }, [ws.name]);

  return (
    <>
      <h2>Sources</h2>
      <p className="sub">The connector registry as declared: every legacy system this workspace
        ingests, with its format and layout. Connectors are declarative specs; the same bronze
        path lands all of them.</p>
      <p className="note" style={{ marginBottom: 16 }}>
        Kinds this build can land, each fixture-proven byte-identical to the local path:
        fixed_width, csv, jsonl, sqlite, s3 (object stores), adbc (sqlite driver; postgresql
        code-present but unclaimed until fixtured), idoc (SAP flat files; OData and RFC not
        claimed), stream_window (recorded segments; live Kafka not claimed). CDC and scheduled
        pulls remain roadmap.</p>
      <Err e={err} />
      <div className="scroll"><table>
        <thead><tr><th>source</th><th>system</th><th>kind</th><th>where</th><th></th></tr></thead>
        <tbody>
          {sources.map((s) => (
            <tr key={s.name}>
              <td className="mono">{s.name}</td>
              <td>{s.label ?? s.system}</td>
              <td><span className="badge hold">{s.kind}</span></td>
              <td className="mono">{s.file ?? s.glob ?? s.db ?? ""}</td>
              <td>{s.layout && (
                <button className="btn ghost" onClick={() => setOpen(open === s.name ? null : s.name)}>
                  {open === s.name ? "hide layout" : "copybook layout"}
                </button>
              )}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      {open && (
        <>
          <h3>{open}: fixed-width layout</h3>
          <div className="scroll"><table>
            <thead><tr><th>field</th><th>width</th></tr></thead>
            <tbody>{(sources.find((s) => s.name === open)?.layout ?? []).map(([f, w]) => (
              <tr key={f}><td className="mono">{f}</td><td className="mono">{w}</td></tr>))}
            </tbody>
          </table></div>
        </>
      )}
    </>
  );
}
