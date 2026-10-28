import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

export default function Ontology({ ws }) {
  const [desc, setDesc] = useState(null);
  const [type, setType] = useState(null);
  const [objs, setObjs] = useState([]);
  const [detail, setDetail] = useState(null);
  const [asofTs, setAsofTs] = useState("");
  const [asofOut, setAsofOut] = useState(null);
  const [escalated, setEscalated] = useState(null);
  const [err, setErr] = useState(null);

  async function escalate(a) {
    setErr(null); setEscalated(null);
    const rule = a.reason?.startsWith("forbidden by ")
      ? a.reason.slice("forbidden by ".length) : "default-deny";
    try {
      const out = await api.escalate(ws.name, {
        principal: detail._as, action: a.action,
        object_id: detail._id, denying_rule: rule,
      });
      setEscalated(out);
    } catch (e) { setErr(e); }
  }

  async function readAsOf() {
    setErr(null); setAsofOut(null);
    try { setAsofOut(await api.asOf(ws.name, type, detail._id, asofTs)); } catch (e) { setErr(e); }
  }

  useEffect(() => {
    setType(null); setObjs([]); setDetail(null); setErr(null);
    api.ontology(ws.name).then(setDesc).catch(setErr);
  }, [ws.name]);

  async function pick(t) {
    setType(t); setDetail(null); setErr(null);
    try { setObjs(await api.objects(ws.name, t)); } catch (e) { setErr(e); setObjs([]); }
  }

  async function open(id) {
    setErr(null); setAsofOut(null); setAsofTs(""); setEscalated(null);
    try { setDetail(await api.objectDashboard(ws.name, type, id)); } catch (e) { setErr(e); }
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
                  <tr key={o._id} onClick={() => open(o._id)} style={{ cursor: "pointer" }}>
                    <td><button className="linklike" onClick={(e) => { e.stopPropagation(); open(o._id); }}>
                      {o._id}</button></td>
                    <td>{o._title}</td><td className="mono">{o._version}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
          {detail && (
            <>
              <div className="row">
                <button className="btn ghost" onClick={() => setDetail(null)}>← back to {type}</button>
                <span className="mono">{detail._id}</span>
                <b>{detail._title}</b>
              </div>
              <h3>Properties (provenance per field)</h3>
              <dl className="kv">
                {Object.entries(detail).filter(([k]) => !k.startsWith("_")).map(([k, v]) => (
                  [<dt key={`${k}t`}>{k}{detail._prov?.[k] ? ` · from ${detail._prov[k]}` : ""}</dt>,
                   <dd key={`${k}d`}>{v === null ? "∅ masked or empty" : String(v)}</dd>]
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

              <h3>What may {detail._as} do here?</h3>
              <div className="row">
                {(detail._allowed_actions ?? []).map((a) => (
                  <span key={a.action} style={{ whiteSpace: "nowrap" }}>
                    <span className={`badge ${a.allowed ? "ok" : "bad"}`} title={a.reason}>
                      {a.action}{a.allowed ? (a.requires_approval ? " · needs approval" : "") : " · denied"}
                    </span>
                    {!a.allowed && (
                      <button className="linklike" onClick={() => escalate(a)}
                              style={{ marginLeft: 4 }}
                              title="raise this exact denial to the owning role">escalate</button>
                    )}
                  </span>
                ))}
              </div>
              {escalated && (
                <p aria-live="polite" className="note">
                  escalated as <span className="mono">{escalated.id}</span> to{" "}
                  <span className="mono">{escalated.owner_role}</span>; it waits in OPERATE,
                  Approvals. {escalated.note}
                </p>
              )}
              <p className="note">Derived from live policy dry-runs for your identity against
                this object, grants and generated policy included; a denied action shows its
                rule on hover and can be escalated to the owning role from here.</p>

              <h3>Audit trail on this object</h3>
              {(detail._trail ?? []).length === 0
                ? <p className="note">No recorded actions touch this object yet.</p>
                : <div className="scroll"><table>
                    <thead><tr><th>seq</th><th>actor</th><th>action</th><th>status</th><th>approved by</th></tr></thead>
                    <tbody>{detail._trail.map((r) => (
                      <tr key={r.seq}><td className="mono">{r.seq}</td>
                        <td className="mono">{r.actor}{r.on_behalf_of ? ` (for ${r.on_behalf_of})` : ""}</td>
                        <td className="mono">{r.action}</td>
                        <td><span className={`badge ${r.status === "needs_approval" ? "hold" : r.status === "denied" ? "bad" : "ok"}`}>{r.status}</span></td>
                        <td className="mono">{r.approved_by ?? ""}</td></tr>))}
                    </tbody>
                  </table></div>}
              {(detail._pending ?? []).length > 0 && (
                <p className="note">{detail._pending.length} approval(s) pending on this object;
                  decide them under OPERATE, Approvals.</p>
              )}
              <h3>Read as of a moment</h3>
              <div className="row">
                <input aria-label="timestamp, ISO or unix" onChange={(e) => setAsofTs(e.target.value)}
                       placeholder="2026-10-15T00:00:00Z or unix seconds" style={{ minWidth: 230 }}
                       type="text" value={asofTs} />
                <button className="btn ghost" disabled={!asofTs.trim()} onClick={readAsOf}>Read as of</button>
              </div>
              {asofOut && (
                <p aria-live="polite" className="note">
                  version {asofOut.version} answered ·{" "}
                  <span className="mono">{JSON.stringify(
                    Object.fromEntries(Object.entries(asofOut.object).filter(([k]) => !k.startsWith("_"))))}</span>
                </p>
              )}

              <h3>Version history</h3>
              {(detail._history ?? []).length === 0
                ? <p className="note">Version 1; never changed since it was built.</p>
                : <p className="mono">{detail._history.map((h) => `v${h.version} by ${h.changed_by ?? h.action_run ?? "build"}`).join("  ·  ")}</p>}
            </>
          )}
        </div>
      </div>
    </>
  );
}
