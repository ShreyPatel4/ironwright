import { useEffect, useState } from "react";
import { api, setPrincipal } from "../api.js";
import { Err } from "../components.jsx";

/* The landing: what the platform is, and the invite-only door. Accounts
   are local salted hashes seeded by the operator (ironwright auth-set);
   the gate guards the console door while the API stays locally open, and
   the page says so because the Platform map's auth node is still PLANNED.
   An unseeded instance says how to seed instead of pretending. */

export default function Login({ onEnter }) {
  const [status, setStatus] = useState(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => { api.authStatus().then(setStatus).catch(setErr); }, []);

  async function enter(e) {
    e?.preventDefault();
    setBusy(true); setErr(null);
    try {
      const s = await api.login(email, password);
      setPrincipal(s.principal);
      onEnter(s);
    } catch (e2) { setErr(e2); }
    setBusy(false);
  }

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
        about themselves on the way. Every number on every screen comes from a real run on
        this machine.
      </p>
      <p className="note" style={{ margin: "0 0 22px" }}>
        Invite only. Your account decides the identity every action is recorded under.
      </p>

      {status && !status.seeded ? (
        <div style={{ background: "var(--bg-1)", border: "1px solid var(--rule)",
                      borderRadius: "var(--rad)", padding: "14px 16px" }}>
          <b style={{ fontSize: 14 }}>No accounts exist on this instance yet.</b>
          <p className="note" style={{ margin: "6px 0 0" }}>
            Seed one from the terminal, then reload:
            <br /><span className="mono">uv run ironwright auth-set you@example.com</span>
            <br />Credentials are salted hashes under your data root; nothing is stored in
            the repository and nothing leaves this machine.
          </p>
        </div>
      ) : (
        <form onSubmit={enter} style={{ background: "var(--bg-1)", border: "1px solid var(--rule)",
                                        borderRadius: "var(--rad)", maxWidth: 420,
                                        padding: "16px 18px" }}>
          <label className="note" htmlFor="login-email">email</label><br />
          <input autoComplete="username" autoFocus id="login-email"
                 onChange={(e) => setEmail(e.target.value)}
                 placeholder="you@company.com" style={{ marginBottom: 10, width: "100%" }}
                 type="text" value={email} /><br />
          <label className="note" htmlFor="login-pass">password</label><br />
          <input autoComplete="current-password" id="login-pass"
                 onChange={(e) => setPassword(e.target.value)}
                 placeholder="your invite password" style={{ marginBottom: 14, width: "100%" }}
                 type="password" value={password} /><br />
          <button className="btn" disabled={busy || !email || !password} type="submit">
            {busy ? "Checking…" : "Enter the console"}
          </button>
        </form>
      )}
      <Err e={err} />
      <p className="note" style={{ marginTop: 16 }}>
        This gate guards the console on this machine; hosted authentication is a planned node
        on the Platform map and this page will say so until it is live.
      </p>
    </div>
  );
}
