import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

function Tree({ roles }) {
  const byParent = {};
  roles.forEach((r) => { (byParent[r.reports_to ?? ""] ||= []).push(r); });
  const lines = [];
  (function walk(parent, depth) {
    for (const r of byParent[parent] ?? []) {
      lines.push(
        <div key={r.id}>
          {" ".repeat(depth * 3)}{depth > 0 ? "└ " : ""}{r.title}
          <span className="muted"> · {r.id}</span>
          {r.approves?.length ? <span className="muted"> · approves {r.approves.join(", ")}</span> : null}
        </div>,
      );
      walk(r.id, depth + 1);
    }
  })("", 0);
  return <div className="tree">{lines}</div>;
}

export default function Organization({ ws, onDeleted }) {
  const [org, setOrg] = useState(null);
  const [confirming, setConfirming] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    setOrg(null); setConfirming(false); setErr(null);
    api.organization(ws.name).then(setOrg).catch(setErr);
  }, [ws.name]);

  async function remove() {
    if (!confirming) { setConfirming(true); return; }
    try { await api.deleteWorkspace(ws.name); onDeleted(); } catch (e) { setErr(e); }
  }

  if (err) return <Err e={err} />;
  if (!org) return <p className="note">Loading the organization…</p>;

  return (
    <>
      <h2>{org.company}</h2>
      <p className="sub">Generated from seed {org.seed} under the {org.archetype} archetype:
        hierarchy, departments, data ownership and approval chains, byte-identical on every
        regeneration of the same spec. The systems estate and runnable packs are the scenario
        engine's next slices; until they exist this workspace is the org layer, and says so.</p>

      <h3>Hierarchy ({org.roles.length} roles)</h3>
      <Tree roles={org.roles} />

      <h3>Departments and data ownership</h3>
      <div className="scroll"><table>
        <thead><tr><th>department</th><th>accountable role</th></tr></thead>
        <tbody>{org.departments.map((d) => (
          <tr key={d.id}><td>{d.name}</td><td className="mono">{d.owner_role}</td></tr>))}
        </tbody>
      </table></div>
      <h3>Who owns which data</h3>
      <div className="scroll"><table>
        <thead><tr><th>object type</th><th>owner role</th></tr></thead>
        <tbody>{Object.entries(org.data_owners).map(([k, v]) => (
          <tr key={k}><td className="mono">{k}</td><td className="mono">{v}</td></tr>))}
        </tbody>
      </table></div>

      <h3>Approval chains</h3>
      <div className="scroll"><table>
        <thead><tr><th>action</th><th>chain, in order</th></tr></thead>
        <tbody>{org.approver_chains.map((c) => (
          <tr key={c.action}><td className="mono">{c.action}</td>
            <td className="mono">{c.chain.join(" → ")}</td></tr>))}
        </tbody>
      </table></div>

      <h3>Footprint</h3>
      <div className="scroll"><table>
        <thead><tr><th>region</th><th>manager</th><th>stores</th></tr></thead>
        <tbody>{org.regions.map((r) => (
          <tr key={r.code}><td>{r.name} ({r.code})</td><td className="mono">{r.manager_role}</td>
            <td className="num">{org.stores.filter((s) => s.region === r.code).length}</td></tr>))}
        </tbody>
      </table></div>

      <div className="row" style={{ marginTop: 26 }}>
        <button className="btn danger" onClick={remove}>
          {confirming ? "Click again to delete this organization" : "Delete organization"}
        </button>
        {confirming && <span className="note">Deletes the generated files under your data root.
          Packs cannot be deleted; this can, because you made it.</span>}
      </div>
    </>
  );
}
