// Same-origin API client. Every answer on screen comes from these calls;
// nothing is computed client-side except rendering. The chosen principal
// rides every request as X-Principal: an identity claim the policy engine
// evaluates, recorded in the audit log, and NOT authentication.
let principal = "user:console";
export const setPrincipal = (p) => { principal = p || "user:console"; };
export const getPrincipal = () => principal;

async function req(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "x-principal": principal, ...(opts.headers || {}) },
  });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail ?? detail; } catch { /* keep statusText */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return r.json();
}

const post = (path, body) =>
  req(path, { method: "POST", headers: { "content-type": "application/json" },
              body: JSON.stringify(body) });

export const api = {
  workspaces: () => req("/workspaces"),
  run: (ws, body = {}) => post(`/workspaces/${ws}/runs`, body),
  approvals: (ws) => req(`/workspaces/${ws}/approvals`),
  decide: (ws, seq, body) => post(`/workspaces/${ws}/approvals/${seq}/decide`, body),
  audit: (ws, limit = 100) => req(`/workspaces/${ws}/audit?limit=${limit}`),
  auditVerify: (ws) => req(`/workspaces/${ws}/audit/verify`),
  ontology: (ws) => req(`/workspaces/${ws}/ontology`),
  objects: (ws, type, limit = 50) => req(`/workspaces/${ws}/objects/${type}?limit=${limit}`),
  object: (ws, type, id) => req(`/workspaces/${ws}/objects/${type}/${encodeURIComponent(id)}`),
  lakeTables: (ws) => req(`/workspaces/${ws}/lake/tables`),
  lakeQuery: (ws, sql, limit = 200) => post(`/workspaces/${ws}/lake/query`, { sql, limit }),
  report: (ws, name) => req(`/workspaces/${ws}/reports/${name}`),
  ask: (ws, question) => post(`/workspaces/${ws}/ask`, { question }),
  askLog: (ws) => req(`/workspaces/${ws}/ask/log`),
  askMetrics: (ws) => req(`/workspaces/${ws}/ask/metrics`),
  askReplay: (ws, index) => post(`/workspaces/${ws}/ask/replay`, { index }),
  createWorkspace: (body) => post("/workspaces", body),
  deleteWorkspace: (ws) => req(`/workspaces/${ws}`, { method: "DELETE" }),
  organization: (ws) => req(`/workspaces/${ws}/organization`),
  archetypes: () => req("/archetypes"),
  sources: (ws) => req(`/workspaces/${ws}/sources`),
  loads: (ws) => req(`/workspaces/${ws}/loads`),
  governance: (ws) => req(`/workspaces/${ws}/governance`),
  governanceCheck: (ws, body) => post(`/workspaces/${ws}/governance/check`, body),
  principals: (ws) => req(`/workspaces/${ws}/principals`),
  agents: (ws) => req(`/workspaces/${ws}/agents`),
  traces: (ws) => req(`/workspaces/${ws}/traces`),
  trace: (ws, id) => req(`/workspaces/${ws}/traces/${id}`),
};
