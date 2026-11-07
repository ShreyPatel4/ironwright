import { useState } from "react";
import { api, setPrincipal } from "../../api.js";
import { Err } from "../../components.jsx";
import { Panel } from "./shared.jsx";

/* Joining an org from an invite link. The server knows the email and the role
   from the token, so all it needs from you is a password. */
export default function Join({ token, onEnter }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [joined, setJoined] = useState(false);

  async function join(e) {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      const s = await api.acceptInvite(token, password);
      if (s.mfa_required) {
        // A known account with two-step sign in: it has joined, and signs in as usual.
        setJoined(true);
        setBusy(false);
        return;
      }
      setPrincipal(s.principal);
      onEnter(s);
    } catch (e2) { setErr(e2); setBusy(false); }
  }

  return (
    <div style={{ "--rad": "6px", margin: "8vh auto 0", maxWidth: 440, padding: "0 16px" }}>
      <h2>Join your team on Ironwright</h2>
      <p className="sub">You have an invite. If this email already has an account, use its password.</p>
      {joined ? (
        <Panel>
          <p style={{ margin: "0 0 12px" }}>You have joined. Your account uses two-step sign in, so sign in
            with your password and your code.</p>
          <button className="btn" onClick={() => { history.replaceState(null, "", location.pathname); location.reload(); }}
                  type="button">Go to sign in</button>
        </Panel>
      ) : (
      <form onSubmit={join}>
        <Panel>
          <label className="note" htmlFor="join-pass">choose a password</label><br />
          <input autoComplete="new-password" autoFocus id="join-pass"
                 onChange={(e) => setPassword(e.target.value)}
                 placeholder="at least 12 characters"
                 style={{ marginBottom: 12, width: "100%" }} type="password" value={password} />
          <button className="btn" disabled={busy || !password} type="submit">
            {busy ? "Joining…" : "Join the org"}
          </button>
        </Panel>
      </form>
      )}
      <Err e={err} />
    </div>
  );
}
