import { useEffect, useState } from "react";
import { api, setPrincipal } from "../api.js";
import { Err } from "../components.jsx";

/* The landing: what the platform is, and the door. A new team signs up and
   gets an org it owns. Someone in more than one org picks which one to work in
   before the console opens, and the server rotates the session into it. */

export default function Login({ onEnter }) {
  const [status, setStatus] = useState(null);
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [orgName, setOrgName] = useState("");
  const [picking, setPicking] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => { api.authStatus().then(setStatus).catch(setErr); }, []);

  function enterAs(s) {
    setPrincipal(s.principal);
    onEnter(s);
  }

  async function enter(e) {
    e?.preventDefault();
    setBusy(true); setErr(null);
    try {
      const s = mode === "signup"
        ? await api.signup(email, password, orgName)
        : await api.login(email, password);
      if ((s.orgs?.length ?? 0) > 1) setPicking(s);
      else enterAs(s);
    } catch (e2) { setErr(e2); }
    setBusy(false);
  }

  async function pick(slug) {
    setBusy(true); setErr(null);
    try {
      enterAs(slug === picking.org_slug ? picking : await api.switchOrg(slug, picking.token));
    } catch (e2) { setErr(e2); }
    setBusy(false);
  }

  const signingUp = mode === "signup";
  const panel = { background: "var(--bg-1)", border: "1px solid var(--rule)",
                  borderRadius: "var(--rad)", maxWidth: 420, padding: "16px 18px" };

  return (
    <div style={{ margin: "8vh auto 0", maxWidth: 560, padding: "0 16px" }}>
      <p className="note mono" style={{ letterSpacing: ".08em", textTransform: "uppercase" }}>
        ironwright
      </p>
      <h2 style={{ fontSize: 26, letterSpacing: "-.01em", margin: "8px 0 10px" }}>
        Legacy systems to an ontology, with agents that are safe to let act.
      </h2>
      <p style={{ color: "var(--ink-1)", fontSize: 14.5, lineHeight: 1.7, margin: "0 0 6px" }}>
        One platform that walks an estate from mainframe files to a governed, queryable,
        agent-ready model of the business, and tells you what your systems did not know
        about themselves on the way.
      </p>
      <p className="note" style={{ margin: "0 0 22px" }}>
        Your account decides the identity every action is recorded under. Your org's data is
        visible to your org and nobody else.
      </p>

      {picking ? (
        <div style={panel}>
          <b style={{ fontSize: 14 }}>Which org do you want to work in?</b>
          <div style={{ marginTop: 10 }}>
            {picking.orgs.map((o) => (
              <button className="btn" disabled={busy} key={o.slug} onClick={() => pick(o.slug)}
                      style={{ display: "block", marginBottom: 8, textAlign: "left", width: "100%" }}
                      type="button">
                {o.name} <span className="note mono">{o.role}</span>
              </button>
            ))}
          </div>
        </div>
      ) : (
        <form onSubmit={enter} style={panel}>
          {signingUp && (
            <>
              <label className="note" htmlFor="login-org">org name</label><br />
              <input autoFocus id="login-org" onChange={(e) => setOrgName(e.target.value)}
                     placeholder="Acme Claims" style={{ marginBottom: 10, width: "100%" }}
                     type="text" value={orgName} /><br />
            </>
          )}
          <label className="note" htmlFor="login-email">email</label><br />
          <input autoComplete="username" autoFocus={!signingUp} id="login-email"
                 onChange={(e) => setEmail(e.target.value)}
                 placeholder="you@company.com" style={{ marginBottom: 10, width: "100%" }}
                 type="text" value={email} /><br />
          <label className="note" htmlFor="login-pass">password</label><br />
          <input autoComplete={signingUp ? "new-password" : "current-password"} id="login-pass"
                 onChange={(e) => setPassword(e.target.value)}
                 placeholder={signingUp ? "at least 8 characters" : "your password"}
                 style={{ marginBottom: 14, width: "100%" }}
                 type="password" value={password} /><br />
          <button className="btn" disabled={busy || !email || !password || (signingUp && !orgName.trim())}
                  type="submit">
            {busy ? "Checking…" : signingUp ? "Create the org" : "Enter the console"}
          </button>
          <button className="btn" onClick={() => { setMode(signingUp ? "login" : "signup"); setErr(null); }}
                  style={{ marginLeft: 8 }} type="button">
            {signingUp ? "I have an account" : "Sign up"}
          </button>
        </form>
      )}
      <Err e={err} />
      {status && !status.seeded && !picking && (
        <p className="note" style={{ marginTop: 16 }}>
          No accounts exist on this instance yet. Sign up to make the first org.
        </p>
      )}
    </div>
  );
}
