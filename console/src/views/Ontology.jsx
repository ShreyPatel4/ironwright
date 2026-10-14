import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

export default function Ontology({ ws }) {
  const [desc, setDesc] = useState(null);
  const [type, setType] = useState(null);
  const [objs, setObjs] = useState([]);
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    setType(null); setObjs([]); setDetail(null); setErr(null);
    api.ontology(ws.name).then(setDesc).catch(setErr);
  }, [ws.name]);

  async function pick(t) {
    setType(t); setDetail(null); setErr(null);
    try { setObjs(await api.objects(ws.name, t)); } catch (e) { setErr(e); setObjs([]); }
  }

  async function open(id) {
    setErr(null);
    try { setDetail(await api.object(ws.name, type, id)); } catch (e) { setErr(e); }
  }

  const types = desc ? Object.entries(desc.describe.object_types ?? {}) : [];

  return (
    <>
      <h2>Ontology</h2>
      <p className="sub">Objects, links, and the crosswalk behind each identity. Fields are
        masked by the same policy the API enforces; the console sees what user:console may see.</p>
      <Err e={err} />
      <div className="split">
        <div className="list">
          {types.map(([t, d]) => (
            <button aria-current={t === type} key={t} onClick={() => pick(t)}>
              {t}
              <span className="note"> {d.description ? `· ${String(d.description).slice(0, 44)}…` : ""}</span>
            </button>
          ))}
        </div>
        <div>
          {!type && <p className="note">Pick an object type.</p>}
          {type && !detail && (
            <div className="scroll"><table>
              <thead><tr><th>id</th><th>title</th><th>version</th></tr></thead>
              <tbody>
                {objs.map((o) => (
                  <tr key={o.id} onClick={() => open(o.id)} style={{ cursor: "pointer" }}>
                    <td className="mono">{o.id}</td><td>{o.title}</td><td className="mono">{o.version}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
          {detail && (
            <>
              <div className="row">
                <button className="btn ghost" onClick={() => setDetail(null)}>← back to {type}</button>
                <span className="mono">{detail.id}</span>
              </div>
              <h3>Properties</h3>
              <dl className="kv">
                {Object.entries(detail.props ?? {}).map(([k, v]) => (
                  [<dt key={`${k}t`}>{k}</dt>, <dd key={`${k}d`}>{v === null ? "∅" : String(v)}</dd>]
                ))}
              </dl>
              <h3>Crosswalk (where this identity came from)</h3>
              <div className="scroll"><table>
                <thead><tr><th>system</th><th>source key</th><th>method</th><th>confidence</th></tr></thead>
                <tbody>{(detail._crosswalk ?? []).map((c, i) => (
                  <tr key={i}><td>{c.system}</td><td className="mono">{c.source_key}</td>
                    <td>{c.method}</td><td className="mono">{c.confidence}</td></tr>))}
                </tbody>
              </table></div>
              <h3>Neighbors</h3>
              <p className="mono">{(detail._neighbors ?? []).map((n) => JSON.stringify(n)).join("  ") || "none"}</p>
            </>
          )}
        </div>
      </div>
    </>
  );
}
