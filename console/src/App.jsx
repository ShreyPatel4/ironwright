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
import Discovery from "./views/Discovery.jsx";
import Boards from "./views/Boards.jsx";
import OrgPolicy from "./views/OrgPolicy.jsx";
import Onboarding from "./views/Onboarding.jsx";
import Login from "./views/Login.jsx";
import { setPrincipal, getPrincipal } from "./api.js";

const VIEWS = [
  ["data", "Sources", Sources],
  ["pipeline", "Pipeline", Pipeline],
  ["loads", "Loads", Loads],
  ["explorer", "Data explorer", Explorer],
  ["semantic", "Ontology", Ontology],
  ["discovery", "Discovery", Discovery],
  ["ask", "Ask", Ask],
  ["boards", "Boards", Boards],
  ["agents", "Agents", Agents],
  ["approvals", "Approvals", Approvals],
  ["audit", "Audit", Audit],
  ["governance", "Governance", Governance],
  ["principals", "Principals", Principals],
  ["organization", "Organization", Organization],
  ["orgpolicy", "Governance", OrgPolicy],
  ["platform", "Platform", Platform],
];
const PACK_GROUPS = [
  ["MAP", ["platform"]],
  ["DATA", ["data", "pipeline", "loads", "explorer"]],
  ["SEMANTIC", ["discovery", "semantic", "ask"]],
  ["OPERATE", ["boards", "agents", "approvals", "audit"]],
  ["GOVERN", ["governance", "principals"]],
];
const ORG_GROUPS = [["MAP", ["platform"]], ["ORGANIZATION", ["organization", "orgpolicy"]]];

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
        <span>
          <label className="note" htmlFor="org-name">workspace name</label><br />
          <input id="org-name" onChange={(e) => setName(e.target.value)}
                 placeholder="lowercase slug, e.g. acme-goods"
                 style={{ width: 190 }} type="text" value={name} />
        </span>
        <span>
          <label className="note" htmlFor="org-arch">archetype</label><br />
          <select id="org-arch" onChange={(e) => setArchetype(e.target.value)} value={archetype}>
            {(archetypes.length ? archetypes : [{ name: "regional_retail_chain" }]).map((a) => (
              <option key={a.name} value={a.name}>{a.name}</option>
            ))}
          </select>
        </span>
        <span>
          <label className="note" htmlFor="org-size">stores (4 to 64)</label><br />
          <input id="org-size" max={64} min={4} onChange={(e) => setSize(e.target.value)}
                 style={{ width: 86 }} type="number" value={size} />
        </span>
        <span>
          <label className="note" htmlFor="org-seed">seed (same seed, same org)</label><br />
          <input id="org-seed" min={0} onChange={(e) => setSeed(e.target.value)}
                 style={{ width: 110 }} type="number" value={seed} />
        </span>
      </div>
      <div className="row">
        <button className="btn" disabled={busy || !name.trim()} onClick={create}>Create</button>
        <button className="btn ghost" onClick={() => setOpen(false)}>Cancel</button>
      </div>
      <Err e={err} />
    </div>
  );
}

const KIND_LABEL = { pack: "demo company", organization: "your organization" };

function storedPrincipal() {
  try { return localStorage.getItem("iw.principal"); } catch { return null; }
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
  const [session, setSession] = useState(() => {
    try {
      const s = JSON.parse(localStorage.getItem("iw.session") ?? "null");
      if (s?.principal) setPrincipal(s.principal);
      return s;
    } catch { return null; }
  });
  const who = session?.principal ?? null;

  function signIn(s) {
    try { localStorage.setItem("iw.session", JSON.stringify(s)); } catch { /* fine */ }
    setSession(s);
  }
  function signOut() {
    if (session?.token) api.logout(session.token).catch(() => {});
    try { localStorage.removeItem("iw.session"); localStorage.removeItem("iw.last"); } catch { /* fine */ }
    setSession(null); setWsName(null); setView(null);
  }

  const refresh = useCallback(async () => {
    try {
      const w = await api.workspaces();
      setWorkspaces(w);
      setErr(null);
      return w;
    } catch (e) { setErr(e); return []; }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    if (!wsName) return;
    try { localStorage.setItem("iw.last", JSON.stringify({ ws: wsName, view })); } catch { /* fine */ }
  }, [wsName, view]);
  useEffect(() => {
    if (!workspaces || wsName || welcome || !session) return;
    try {
      const last = JSON.parse(localStorage.getItem("iw.last") ?? "null");
      const w = last && workspaces.find((x) => x.name === last.ws);
      if (w) { setWsName(w.name); setView(last.view ?? (w.kind === "organization" ? "organization" : "pipeline")); }
    } catch { /* fine */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaces, session, welcome]);

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

  if (!session) {
    return <main style={{ margin: "0 auto" }}><Login onEnter={signIn} /></main>;
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
        <div className="mark"><b>Ironwright</b>legacy systems to governed agents</div>
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
            <p className="note" style={{ margin: "12px 0 0" }}>
              {session.email}<br />acting as <span className="mono">{who}</span><br />
              <a href="#signout" onClick={(e) => { e.preventDefault(); signOut(); }}>sign out</a>
            </p>
          </div>
        )}
        {!ws && (
          <div className="ws">
            <p className="note" style={{ margin: 0 }}>
              {session.email}<br />acting as <span className="mono">{who}</span><br />
              <a href="#signout" onClick={(e) => { e.preventDefault(); signOut(); }}>sign out</a>
            </p>
          </div>
        )}
      </nav>
      <main>
        <Err e={err} />
        {!ws && workspaces && (
          <>
            <h2>Workspaces</h2>
            <p className="sub">Each workspace is one company's whole migration: its legacy systems,
              lakehouse, ontology, agents and audit trail. The four demo companies are provable
              end to end; the organizations are ones you created. The level beside each is
              derived from artifacts on this machine, never declared.</p>
            <div className="cards" style={{ marginBottom: 18 }}>
              {workspaces.map((w) => (
                <button className="card" key={w.name} onClick={() => openWorkspace(w)}>
                  <span>
                    <span className="kind">{KIND_LABEL[w.kind] ?? w.kind}</span><br />
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
