// Same-origin API client. Every answer on screen comes from these calls;
// nothing is computed client-side except rendering. The session lives in an
// httpOnly cookie that no script here can read. What the page holds is the CSRF
// token, in memory only, and it rides every unsafe request. The chosen principal
// rides along as X-Principal, which only a dev open server listens to.
let principal = "user:console";
export const setPrincipal = (p) => { principal = p || "user:console"; };
export const getPrincipal = () => principal;

let csrf = null;
export const setCsrf = (t) => { csrf = t || null; };
const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

async function req(path, opts = {}) {
  const unsafe = !SAFE.has((opts.method || "GET").toUpperCase());
  const r = await fetch(path, {
    credentials: "same-origin",
    ...opts,
    headers: { "x-principal": principal,
               ...(unsafe && csrf ? { "x-csrf-token": csrf } : {}),
               ...(opts.headers || {}) },
  });
  // A session the server refused has ended: back to the door.
  if (r.status === 401 && csrf && !opts.quiet) {
    csrf = null;
    location.reload();
  }
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail ?? detail; } catch { /* keep statusText */ }
    const failed = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    failed.status = r.status;
    throw failed;
  }
  return r.json();
}

const post = (path, body) =>
  req(path, { method: "POST", headers: { "content-type": "application/json" },
              body: JSON.stringify(body) });

const put = (path, body) =>
  req(path, { method: "PUT", headers: { "content-type": "application/json" },
              body: JSON.stringify(body) });
const org = (slug) => `/orgs/${encodeURIComponent(slug)}`;
// The auth audit: sign in, failures, locks, two-step changes, tokens and people.
const AUTH_EVENTS = "auth.,mfa.,token.,member.,org.require_mfa";

// The cookie rides a fetch, so the file comes through fetch and then a click on a
// throwaway link to the bytes.
async function saveFile(path, name) {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail ?? detail; } catch { /* keep statusText */ }
    throw new Error(detail);
  }
  const url = URL.createObjectURL(await r.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

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
  objectDashboard: (ws, type, id) => req(`/workspaces/${ws}/objects/${type}/${encodeURIComponent(id)}/dashboard`),
  boards: (ws) => req(`/workspaces/${ws}/boards`),
  board: (ws, slug) => req(`/workspaces/${ws}/boards/${slug}`),
  saveBoard: (ws, slug, body) => req(`/workspaces/${ws}/boards/${slug}`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  deleteBoard: (ws, slug) => req(`/workspaces/${ws}/boards/${slug}`, { method: "DELETE" }),
  runBoard: (ws, slug) => post(`/workspaces/${ws}/boards/${slug}/run`, {}),
  orgpolicyPreview: (ws) => req(`/workspaces/${ws}/orgpolicy/preview`),
  orgpolicyApply: (ws, approved_by) => post(`/workspaces/${ws}/orgpolicy/apply`, { approved_by }),
  escalations: (ws) => req(`/workspaces/${ws}/escalations`),
  escalate: (ws, body) => post(`/workspaces/${ws}/escalations`, body),
  decideEscalation: (ws, id, body) => post(`/workspaces/${ws}/escalations/${id}/decide`, body),
  asOf: (ws, type, id, ts) => req(`/workspaces/${ws}/objects/${type}/${encodeURIComponent(id)}/asof?ts=${encodeURIComponent(ts)}`),
  otelExport: (ws, endpoint) => post(`/workspaces/${ws}/otel/export`, { endpoint }),
  lakeTables: (ws) => req(`/workspaces/${ws}/lake/tables`),
  lakeQuery: (ws, sql, limit = 200) => post(`/workspaces/${ws}/lake/query`, { sql, limit }),
  report: (ws, name) => req(`/workspaces/${ws}/reports/${name}`),
  ask: (ws, question) => post(`/workspaces/${ws}/ask`, { question }),
  askLog: (ws) => req(`/workspaces/${ws}/ask/log`),
  askMetrics: (ws) => req(`/workspaces/${ws}/ask/metrics`),
  askExamples: (ws) => req(`/workspaces/${ws}/ask/examples`),
  askReplay: (ws, index) => post(`/workspaces/${ws}/ask/replay`, { index }),
  createWorkspace: (body) => post("/workspaces", body),
  deleteWorkspace: (ws) => req(`/workspaces/${ws}`, { method: "DELETE" }),
  organization: (ws) => req(`/workspaces/${ws}/organization`),
  archetypes: () => req("/archetypes"),
  platformMap: () => req("/platform/map"),
  discovery: (ws) => req(`/workspaces/${ws}/discovery`),
  authStatus: () => req("/auth/status"),
  login: (email, password) => post("/auth/login", { email, password }),
  signup: (email, password, org_name) => post("/auth/signup", { email, password, org_name }),
  // The CSRF token is passed in because Login calls this before it is stored.
  switchOrg: (slug, token) => req(`/orgs/${encodeURIComponent(slug)}/switch`, {
    method: "POST", ...(token ? { headers: { "x-csrf-token": token } } : {}) }),
  me: () => req("/auth/me"),
  sessionWorks: (token) => req("/auth/me", { headers: { authorization: `Bearer ${token}` }, quiet: true }).then(() => true, () => false),
  logout: () => req("/auth/logout", { method: "POST", quiet: true }),
  mfa: (ticket, code, recovery_code) => post("/auth/mfa", recovery_code ? { ticket, recovery_code } : { ticket, code }),
  mfaStatus: () => req("/auth/mfa/status"),
  mfaEnrol: () => post("/auth/mfa/enrol", {}),
  mfaConfirm: (code) => post("/auth/mfa/confirm", { code }),
  mfaDisable: (code) => post("/auth/mfa/disable", { code }),
  mfaNewRecovery: (code) => post("/auth/mfa/recovery-codes", { code }),
  resetRequest: (email) => post("/auth/reset-request", { email }),
  reset: (token, password) => post("/auth/reset", { token, password }),
  verifyEmail: (token) => post("/auth/verify", { token }),
  resendVerify: () => post("/auth/verify/resend", {}),
  sessions: () => req("/auth/sessions"),
  endSession: (id) => req(`/auth/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  signOutEverywhere: () => post("/auth/sessions/revoke-all", {}),
  orgSecurity: (slug) => req(`${org(slug)}/security`),
  setOrgSecurity: (slug, require_mfa) => put(`${org(slug)}/security`, { require_mfa }),
  tokens: (slug) => req(`${org(slug)}/tokens`),
  createToken: (slug, name, scopes) => post(`${org(slug)}/tokens`, { name, scopes }),
  revokeToken: (slug, id) => req(`${org(slug)}/tokens/${encodeURIComponent(id)}`, { method: "DELETE" }),
  authAudit: (slug) => req(`${org(slug)}/audit/chain?prefix=${encodeURIComponent(AUTH_EVENTS)}`),
  exportAudit: (slug) => saveFile(`${org(slug)}/audit/export`, `${slug}-audit.jsonl`),
  sources: (ws) => req(`/workspaces/${ws}/sources`),
  addSources: (ws, body) => post(`/workspaces/${ws}/sources`, body),
  drift: (ws) => req(`/workspaces/${ws}/drift`),
  sourceVersions: (ws, source) => req(`/workspaces/${ws}/sources/${source}/versions`),
  addSourceVersion: (ws, source, upload) => post(`/workspaces/${ws}/sources/${source}/versions`, { upload }),
  updateSourceSpec: (ws, source, body) => post(`/workspaces/${ws}/sources/${source}/spec`, body),
  loads: (ws) => req(`/workspaces/${ws}/loads`),
  governance: (ws) => req(`/workspaces/${ws}/governance`),
  governanceCheck: (ws, body) => post(`/workspaces/${ws}/governance/check`, body),
  principals: (ws) => req(`/workspaces/${ws}/principals`),
  agents: (ws) => req(`/workspaces/${ws}/agents`),
  traces: (ws) => req(`/workspaces/${ws}/traces`),
  trace: (ws, id) => req(`/workspaces/${ws}/traces/${id}`),
  trainingLabels: (ws) => req(`/workspaces/${ws}/training/labels`),
  trainingEvaluate: (ws, rules) => post(`/workspaces/${ws}/training/evaluate`, { rules }),
  enginesCheck: (ws) => post(`/workspaces/${ws}/engines/check`, {}),
  bi: (ws) => req(`/workspaces/${ws}/bi`),
  setBi: (ws, url) => req(`/workspaces/${ws}/bi`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ url }) }),
  biMirror: (ws, schema) => post(`/workspaces/${ws}/bi/mirror`, schema ? { schema } : {}),
  acceptInvite: (token, password) => post("/auth/accept-invite", { token, password }),
  // No content-type here: the browser has to write the multipart boundary itself.
  upload: (files) => {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    return req("/uploads", { method: "POST", body: form });
  },
  uploads: () => req("/uploads"),
  uploadProfile: (id) => req(`/uploads/${id}/profile`),
  startProfile: (id) => post(`/uploads/${id}/profile`, {}),
  uploadProposal: (id) => req(`/uploads/${id}/proposal`),
  createJob: (kind, workspace, params = {}) => post("/jobs", { kind, workspace, params }),
  jobs: (workspace) => req(`/jobs?workspace=${encodeURIComponent(workspace)}`),
  job: (id) => req(`/jobs/${id}`),
  jobLog: (id, offset = 0) => req(`/jobs/${id}/log?offset=${offset}`),
  cancelJob: (id) => post(`/jobs/${id}/cancel`, {}),
  createTenantWorkspace: (body) => post("/workspaces", body),
  silverDraft: (ws) => req(`/workspaces/${ws}/silver/draft`),
  rewriteSilver: (ws, declined, renames) => post(`/workspaces/${ws}/silver/draft`, { declined, renames }),
  ontologyProposal: (ws) => req(`/workspaces/${ws}/ontology/proposal`),
  ontologyAccept: (ws, proposal_id, edits) => post(`/workspaces/${ws}/ontology/accept`, { proposal_id, edits }),
  members: (slug) => req(`${org(slug)}/members`),
  invite: (slug, email, role) => post(`${org(slug)}/invites`, { email, role }),
  setMemberRole: (slug, userId, role) => put(`${org(slug)}/members/${userId}`, { role }),
  removeMember: (slug, userId) => req(`${org(slug)}/members/${userId}`, { method: "DELETE" }),
  usage: (slug) => req(`${org(slug)}/usage`),
  llmKey: (slug) => req(`${org(slug)}/llm-key`),
  setLlmKey: (slug, provider, key) => put(`${org(slug)}/llm-key`, { provider, key }),
  removeLlmKey: (slug) => req(`${org(slug)}/llm-key`, { method: "DELETE" }),
  auditChain: (slug, { actor, action }) => req(`${org(slug)}/audit/chain?${new URLSearchParams({
    ...(actor ? { actor } : {}), ...(action ? { action } : {}) })}`),
  exports: (slug) => req(`${org(slug)}/exports`),
  createExport: (slug) => post(`${org(slug)}/exports`, {}),
  downloadExport: (slug, id) => saveFile(`${org(slug)}/exports/${id}/download`, `${slug}-export.zip`),
  deleteOrg: (slug, confirm) => post(`${org(slug)}/delete`, { confirm }),
};
