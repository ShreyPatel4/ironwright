import { useCallback, useEffect, useState } from "react";
import { Badge, Err } from "./components.jsx";
import { api } from "./api.js";
import Pipeline from "./views/Pipeline.jsx";
import Explorer from "./views/Explorer.jsx";
import Ontology from "./views/Ontology.jsx";
import Approvals from "./views/Approvals.jsx";
import Audit from "./views/Audit.jsx";
import Ask from "./views/Ask.jsx";

const VIEWS = [
  ["pipeline", "Pipeline", Pipeline],
  ["explorer", "Data explorer", Explorer],
  ["ontology", "Ontology", Ontology],
  ["ask", "Ask", Ask],
  ["approvals", "Approvals", Approvals],
  ["audit", "Audit", Audit],
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
        {ws && VIEWS.map(([k, label]) => (
          <button aria-current={view === k} key={k} onClick={() => setView(k)}>{label}</button>
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
