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
import Organization from "./views/Organization.jsx";
import Platform from "./views/Platform.jsx";
import Onboarding from "./views/Onboarding.jsx";

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
  ["organization", "Organization", Organization],
  ["platform", "Platform", Platform],
];
const PACK_GROUPS = [
  ["MAP", ["platform"]],
  ["DATA", ["data", "pipeline", "loads", "explorer"]],
  ["SEMANTIC", ["semantic", "ask"]],
  ["OPERATE", ["agents", "approvals", "audit"]],
  ["GOVERN", ["governance", "principals"]],
];
const ORG_GROUPS = [["MAP", ["platform"]], ["ORGANIZATION", ["organization"]]];

function NewOrganization({ onCreated, startOpen = false }) {
  const [open, setOpen] = useState(startOpen);
  const [name, setName] = useState("");
  const [archetypes, setArchetypes] = useState([]);
  const [archetype, setArchetype] = useState("regional_retail_chain");
  const [size, setSize] = useState(16);
  const [seed, setSeed] = useState(7);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => { api.archetypes().then(setArchetypes).catch(() => {}); }, []);

  async function create() {
    setBusy(true); setErr(null);
    try {
      await api.createWorkspace({ name, archetype, size: Number(size), seed: Number(seed) });
      const made = name;
      setOpen(false); setName("");
      onCreated(made);
    } catch (e) { setErr(e); }
    setBusy(false);
  }

  if (!open) {
    return <button className="btn" onClick={() => setOpen(true)}>New organization</button>;
  }
  return (
    <div style={{ background: "var(--bg-1)", border: "1px solid var(--rule)",
                  borderRadius: "var(--rad)", maxWidth: 560, padding: "16px 18px" }}>
      <h3 style={{ margin: "0 0 4px" }}>New organization</h3>
      <p className="note" style={{ margin: "0 0 12px" }}>
        The scenario engine generates the whole org from a name, an archetype, a size and a
        seed: hierarchy, departments, data owners, approval chains. Same spec, same org,
        byte for byte.
      </p>
      <div className="row">
        <input onChange={(e) => setName(e.target.value)} placeholder="name, e.g. acme-goods"
               style={{ width: 180 }} type="text" value={name} />
        <select onChange={(e) => setArchetype(e.target.value)} value={archetype}>
          {(archetypes.length ? archetypes : [{ name: "regional_retail_chain" }]).map((a) => (
            <option key={a.name} value={a.name}>{a.name}</option>
          ))}
        </select>
        <input max={64} min={4} onChange={(e) => setSize(e.target.value)}
               style={{ width: 76 }} title="stores (4-64)" type="number" value={size} />
        <input min={0} onChange={(e) => setSeed(e.target.value)}
               style={{ width: 96 }} title="seed" type="number" value={seed} />
      </div>
      <div className="row">
        <button className="btn" disabled={busy || !name.trim()} onClick={create}>Create</button>
        <button className="btn ghost" onClick={() => setOpen(false)}>Cancel</button>
      </div>
      <Err e={err} />
    </div>
  );
}

function onboarded() {
  try { return localStorage.getItem("iw.onboarded") === "1"; } catch { return true; }
}
function setOnboarded() {
  try { localStorage.setItem("iw.onboarded", "1"); } catch { /* fine */ }
}

export default function App() {
  const [workspaces, setWorkspaces] = useState(null);
  const [wsName, setWsName] = useState(null);
  const [view, setView] = useState(null);
  const [err, setErr] = useState(null);
  const [welcome, setWelcome] = useState(() => !onboarded());
  const [orgFormOpen, setOrgFormOpen] = useState(false);

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
  const groups = ws?.kind === "organization" ? ORG_GROUPS : PACK_GROUPS;
  const Active = VIEWS.find(([k]) => k === view)?.[2];

  function openWorkspace(w) {
    setWsName(w.name);
    setView(w.kind === "organization" ? "organization" : "pipeline");
  }

  function chooseStart(id) {
    setOnboarded(); setWelcome(false);
    const retail = workspaces?.find((w) => w.name === "retail") ?? workspaces?.[0];
    if (id === "demo" && retail) { openWorkspace(retail); return; }
    if (id === "org") { setOrgFormOpen(true); return; }
    if ((id === "byod" || id === "map") && retail) { setWsName(retail.name); setView("platform"); }
  }

  if (welcome && workspaces) {
    return (
      <main style={{ margin: "0 auto" }}>
        <Onboarding onChoose={chooseStart}
                    onSkip={() => { setOnboarded(); setWelcome(false); }} />
      </main>
    );
  }

  return (
    <>
      <nav className="rail">
        <div className="mark"><b>Ironwright</b>console</div>
        <button aria-current={view === null}
                onClick={() => { setView(null); setWsName(null); refresh(); }}>
          Workspaces
        </button>
        {ws && groups.map(([g, keys]) => (
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
            <div className="note" style={{ marginBottom: 4 }}>workspace</div>
            <select onChange={(e) => openWorkspace(workspaces.find((w) => w.name === e.target.value))}
                    value={ws.name}>
              {workspaces.map((w) => <option key={w.name} value={w.name}>{w.name}</option>)}
            </select>
            <div style={{ marginTop: 10 }}><Badge v={ws.verification} /></div>
          </div>
        )}
      </nav>
      <main>
        <Err e={err} />
        {!ws && workspaces && (
          <>
            <h2>Workspaces</h2>
            <p className="sub">Four shipped packs, provable end to end, and the organizations
              you create yourself. The level beside each is derived from artifacts on this
              machine, never declared.</p>
            <div className="cards" style={{ marginBottom: 18 }}>
              {workspaces.map((w) => (
                <button className="card" key={w.name} onClick={() => openWorkspace(w)}>
                  <span>
                    <span className="kind">{w.kind}</span><br />
                    <b>{w.title}</b>
                  </span>
                  <p>{w.decision}</p>
                  <Badge v={w.verification} />
                </button>
              ))}
            </div>
            <NewOrganization onCreated={async (name) => {
              setOrgFormOpen(false);
              const w = await refresh();
              const created = w.find((x) => x.name === name);
              if (created) openWorkspace(created);
            }} startOpen={orgFormOpen} />
            <p className="note" style={{ marginTop: 16 }}>
              New here? <a href="#welcome" onClick={(e) => { e.preventDefault(); setWelcome(true); }}>
              Replay the welcome tour</a>.
            </p>
          </>
        )}
        {ws && Active && (
          <Active onDeleted={async () => { setWsName(null); setView(null); await refresh(); }}
                  refresh={refresh} setView={setView} ws={ws} />
        )}
      </main>
    </>
  );
}
