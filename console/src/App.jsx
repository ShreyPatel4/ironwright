import { useCallback, useEffect, useState } from "react";
import { Badge, Err } from "./components.jsx";
import { api } from "./api.js";
import Pipeline from "./views/Pipeline.jsx";
import Explorer from "./views/Explorer.jsx";
import Ontology from "./views/Ontology.jsx";
import Approvals from "./views/Approvals.jsx";
import Audit from "./views/Audit.jsx";
import Ask from "./views/Ask.jsx";
import Sources from "./views/Sources.jsx";
import Loads from "./views/Loads.jsx";
import Agents from "./views/Agents.jsx";
import Governance from "./views/Governance.jsx";
import Principals from "./views/Principals.jsx";

const VIEWS = [
  ["data", "Sources", Sources],
  ["pipeline", "Pipeline", Pipeline],
  ["loads", "Loads", Loads],
  ["explorer", "Data explorer", Explorer],
  ["semantic", "Ontology", Ontology],
  ["ask", "Ask", Ask],
  ["agents", "Agents", Agents],
  ["approvals", "Approvals", Approvals],
  ["audit", "Audit", Audit],
  ["governance", "Governance", Governance],
  ["principals", "Principals", Principals],
];
const GROUPS = [
  ["DATA", ["data", "pipeline", "loads", "explorer"]],
  ["SEMANTIC", ["semantic", "ask"]],
  ["OPERATE", ["agents", "approvals", "audit"]],
  ["GOVERN", ["governance", "principals"]],
];

export default function App() {
  const [workspaces, setWorkspaces] = useState(null);
  const [wsName, setWsName] = useState(null);
  const [view, setView] = useState(null); // null = workspaces home
  const [err, setErr] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const w = await api.workspaces();
      setWorkspaces(w);
      setErr(null);
      return w;
    } catch (e) { setErr(e); return []; }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  const ws = workspaces?.find((w) => w.name === wsName) ?? null;
  const Active = VIEWS.find(([k]) => k === view)?.[2];

  return (
    <>
      <nav className="rail">
        <div className="mark"><b>Ironwright</b>console</div>
        <button aria-current={view === null} onClick={() => { setView(null); setWsName(null); }}>
          Workspaces
        </button>
        {ws && GROUPS.map(([g, keys]) => (
          <div key={g}>
            <div className="railgroup">{g}</div>
            {keys.map((k) => {
              const [, label] = VIEWS.find(([vk]) => vk === k);
              return <button aria-current={view === k} key={k} onClick={() => setView(k)}>{label}</button>;
            })}
          </div>
        ))}
        {ws && (
          <div className="ws">
            <span className="note">workspace</span>
            <select onChange={(e) => setWsName(e.target.value)} value={ws.name}>
              {workspaces.map((w) => <option key={w.name} value={w.name}>{w.name}</option>)}
            </select>
            <div style={{ marginTop: 8 }}><Badge v={ws.verification} /></div>
          </div>
        )}
      </nav>
      <main>
        <Err e={err} />
        {!ws && workspaces && (
          <>
            <h2>Workspaces</h2>
            <p className="sub">Four fictional companies, each a full migration with planted
              defects and ground truth. The badge under each is derived from artifacts on this
              machine: L3 only when ground truth exists and the evals are green here.</p>
            <div className="cards">
              {workspaces.map((w) => (
                <button className="card" key={w.name}
                        onClick={() => { setWsName(w.name); setView("pipeline"); }}>
                  <b>{w.title}</b>
                  <p>{w.decision}</p>
                  <span><Badge v={w.verification} /></span>
                </button>
              ))}
            </div>
          </>
        )}
        {ws && Active && <Active refresh={refresh} ws={ws} />}
      </main>
    </>
  );
}
