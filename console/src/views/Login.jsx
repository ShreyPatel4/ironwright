import { useEffect, useState } from "react";
import { api, setPrincipal } from "../api.js";
import { Err } from "../components.jsx";

/* The landing: what the platform is, and the door. A new team signs up and
   gets an org it owns. Someone in more than one org picks which one to work in
   before the console opens, and the server rotates the session into it. An account
   with two-step sign in gets a code box after the password, and only the code opens
   a session. */

export default function Login({ onEnter }) {
  const [status, setStatus] = useState(null);
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [orgName, setOrgName] = useState("");
  const [picking, setPicking] = useState(null);
  const [ticket, setTicket] = useState(null);
  const [code, setCode] = useState("");
  const [useRecovery, setUseRecovery] = useState(false);
  const [sent, setSent] = useState(null);
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
      // An email with an account gets an answer that looks like a sign up and a session
      // that does not work. Say what to do before it bounces back here with no word.
      if (mode === "signup" && !(await api.sessionWorks(s.token))) {
        setMode("login");
        setErr(new Error("If this email already has an account, sign in with its password."));
        setBusy(false);
        return;
      }
      if (s.mfa_required) { setTicket(s.ticket); setCode(""); setBusy(false); return; }
      opened(s);
    } catch (e2) { setErr(e2); }
    setBusy(false);
  }

  function opened(s) {
    if ((s.orgs?.length ?? 0) > 1) setPicking(s);
    else enterAs(s);
  }

  async function second(e) {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const s = await api.mfa(ticket, useRecovery ? null : code, useRecovery ? code : null);
      setTicket(null);
      opened(s);
    } catch (e2) {
      setErr(e2);
      if (e2.status === 429) setTicket(null);
    }
    setBusy(false);
  }

  async function askReset(e) {
    e.preventDefault();
    setBusy(true); setErr(null);
    try { setSent((await api.resetRequest(email)).detail); } catch (e2) { setErr(e2); }
    setBusy(false);
  }

  async function pick(slug) {
    setBusy(true); setErr(null);
    try {
      enterAs(slug === picking.org_slug ? picking : await api.switchOrg(slug, picking.csrf));
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

      {mode === "forgot" ? (
        <form onSubmit={askReset} style={panel}>
          <b style={{ fontSize: 14 }}>Reset your password</b>
          <p className="note" style={{ margin: "6px 0 12px" }}>
            I will mail a link to this address if it has an account. The link works once, for 30 minutes.
          </p>
          <label className="note" htmlFor="forgot-email">email</label><br />
          <input autoFocus id="forgot-email" onChange={(e) => setEmail(e.target.value)}
                 placeholder="you@company.com" style={{ marginBottom: 12, width: "100%" }}
                 type="text" value={email} /><br />
          <button className="btn" disabled={busy || !email} type="submit">Send the link</button>
          <button className="btn" onClick={() => { setMode("login"); setSent(null); setErr(null); }}
                  style={{ marginLeft: 8 }} type="button">Back to sign in</button>
          {sent && <p className="note" data-testid="reset-sent" style={{ marginTop: 12 }}>{sent}.</p>}
        </form>
      ) : ticket ? (
        <form onSubmit={second} style={panel}>
          <b style={{ fontSize: 14 }}>Two-step sign in</b>
          <p className="note" style={{ margin: "6px 0 12px" }}>
            {useRecovery ? "Type one of your recovery codes. Each one works once."
                         : "Type the 6 digit code from your authenticator app."}
          </p>
          <label className="note" htmlFor="mfa-code">{useRecovery ? "recovery code" : "code"}</label><br />
          <input autoComplete="one-time-code" autoFocus id="mfa-code" inputMode={useRecovery ? "text" : "numeric"}
                 onChange={(e) => setCode(e.target.value)} placeholder={useRecovery ? "xxxxxxxx-xxxxxxxx" : "123456"}
                 style={{ marginBottom: 12, width: "100%" }} type="text" value={code} /><br />
          <button className="btn" disabled={busy || !code.trim()} type="submit">
            {busy ? "Checking…" : "Sign in"}
          </button>
          <button className="btn" onClick={() => { setUseRecovery(!useRecovery); setCode(""); setErr(null); }}
                  style={{ marginLeft: 8 }} type="button">
            {useRecovery ? "Use a code instead" : "Use a recovery code"}
          </button>
        </form>
      ) : picking ? (
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
                 placeholder={signingUp ? "at least 12 characters" : "your password"}
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
          {!signingUp && (
            <p className="note" style={{ margin: "12px 0 0" }}>
              <a href="#forgot" onClick={(e) => { e.preventDefault(); setMode("forgot"); setErr(null); }}>
                Forgot your password?</a>
            </p>
          )}
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
