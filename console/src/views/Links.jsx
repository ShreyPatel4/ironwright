import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

/* Where a mailed link lands. The token rides in the fragment, so no server log
   holds it. Each link works once and for 30 minutes. */

const box = { background: "var(--bg-1)", border: "1px solid var(--rule)",
              borderRadius: "var(--rad)", padding: "16px 18px" };

export function ResetPassword({ token, onDone }) {
  const [password, setPassword] = useState("");
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  async function save(e) {
    e.preventDefault();
    setBusy(true); setErr(null);
    try { await api.reset(token, password); setDone(true); } catch (e2) { setErr(e2); }
    setBusy(false);
  }

  return (
    <div style={{ margin: "8vh auto 0", maxWidth: 440, padding: "0 16px" }}>
      <h2>Choose a new password</h2>
      {done ? (
        <div style={box}>
          <p data-testid="reset-done" style={{ margin: "0 0 12px" }}>Your password is changed. Every device
            that was signed in is signed out now, so sign in again.</p>
          <button className="btn" onClick={onDone} type="button">Go to sign in</button>
        </div>
      ) : (
        <form onSubmit={save} style={box}>
          <p className="note" style={{ margin: "0 0 12px" }}>At least 12 characters. A long phrase is easy
            to remember and hard to guess. I check it against a list of breached passwords.</p>
          <label className="note" htmlFor="reset-pass">new password</label><br />
          <input autoComplete="new-password" autoFocus id="reset-pass" onChange={(e) => setPassword(e.target.value)}
                 style={{ marginBottom: 12, width: "100%" }} type="password" value={password} /><br />
          <button className="btn" disabled={busy || !password} type="submit">Save the password</button>
        </form>
      )}
      <Err e={err} />
    </div>
  );
}

export function VerifyEmail({ token, onDone }) {
  const [state, setState] = useState("checking");
  const [err, setErr] = useState(null);

  useEffect(() => {
    api.verifyEmail(token).then(() => setState("done"), (e) => { setErr(e); setState("failed"); });
  }, [token]);

  return (
    <div style={{ margin: "8vh auto 0", maxWidth: 440, padding: "0 16px" }}>
      <h2>Confirm your email</h2>
      <div style={box}>
        <p data-testid="verify-state" style={{ margin: "0 0 12px" }}>
          {state === "checking" ? "Checking the link…"
            : state === "done" ? "Your email is confirmed. Thank you."
            : "That link did not work. Ask for a new one in Settings, Security."}
        </p>
        <button className="btn" onClick={onDone} type="button">Continue</button>
      </div>
      <Err e={err} />
    </div>
  );
}
