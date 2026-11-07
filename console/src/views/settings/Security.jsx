import { useCallback, useEffect, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";

/* Your account's security, and for admins the org's. Every action here is checked
   again by the server, and every change lands in the org's audit chain. */

const RANK = ["viewer", "member", "admin", "owner"];
const SCOPES = [
  ["read", "read: every read outside settings"],
  ["ask", "ask: Ask questions"],
  ["run", "run: start runs and jobs"],
  ["act:request", "act:request: ask for an action, which a person approves"],
];
const when = (ts) => (ts ? new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " ") : "never");
const box = { background: "var(--bg-1)", border: "1px solid var(--rule)", borderRadius: "var(--rad)",
              marginBottom: 16, padding: "14px 16px" };

function Section({ title, children, testid }) {
  return (
    <section data-testid={testid} style={box}>
      <h3 style={{ margin: "0 0 8px" }}>{title}</h3>
      {children}
    </section>
  );
}

function TwoStep({ onRotated, required }) {
  const [status, setStatus] = useState(null);
  const [pending, setPending] = useState(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(() => api.mfaStatus().then(setStatus).catch(setErr), []);
  useEffect(() => { load(); }, [load]);

  async function act(fn) {
    setBusy(true); setErr(null);
    try { await fn(); } catch (e) { setErr(e); }
    setBusy(false);
  }
  const start = () => act(async () => { setPending(await api.mfaEnrol()); setCode(""); });
  const confirm = () => act(async () => {
    const done = await api.mfaConfirm(code);
    setPending(null); setCode(""); setCodes(done.recovery_codes);
    await load();
    // Every other device is signed out, and this one has a new session.
    onRotated?.(done.session, { keep: true });
  });
  const turnOff = () => act(async () => { await api.mfaDisable(code); setCode(""); await load(); });
  const fresh = () => act(async () => { setCodes((await api.mfaNewRecovery(code)).recovery_codes); setCode(""); await load(); });

  return (
    <Section testid="security-mfa" title="Two-step sign in">
      {codes && (
        <div data-testid="recovery-codes" style={{ ...box, borderColor: "var(--accent)" }}>
          <b>Your recovery codes</b>
          <p className="note">Keep them somewhere safe. Each one signs you in once if you lose your phone.
            This is the only time I show them.</p>
          <pre className="mono" style={{ margin: 0 }}>{codes.join("\n")}</pre>
          <button className="btn ghost" onClick={() => setCodes(null)} style={{ marginTop: 10 }}>I saved them</button>
        </div>
      )}
      {status && !status.enabled && !pending && (
        <>
          <p className="sub">Off. With it on, a password alone does not open your account: you also type a
            6 digit code from an authenticator app.</p>
          {required && <p className="note">Your org requires it.</p>}
          <button className="btn" disabled={busy} onClick={start}>Set it up</button>
        </>
      )}
      {pending && (
        <>
          <p className="sub">Add this key to your authenticator app, then type the code it shows.</p>
          <p className="mono" data-testid="mfa-secret" style={{ letterSpacing: ".08em", wordBreak: "break-all" }}>
            {pending.secret.match(/.{1,4}/g).join(" ")}</p>
          <p className="note">Or open <a href={pending.otpauth_uri}>this link</a> on the phone with the app.</p>
          <div className="row">
            <input aria-label="code from the app" autoComplete="one-time-code" inputMode="numeric"
                   onChange={(e) => setCode(e.target.value)} placeholder="123456" type="text" value={code} />
            <button className="btn" disabled={busy || !code.trim()} onClick={confirm}>Turn it on</button>
          </div>
        </>
      )}
      {status?.enabled && (
        <>
          <p className="sub" data-testid="mfa-on">On. {status.recovery_left} recovery codes left.</p>
          {status.required_by.length > 0 && (
            <p className="note">{status.required_by.join(", ")} requires it, so it stays on.</p>)}
          <div className="row">
            <input aria-label="current code" autoComplete="one-time-code" inputMode="numeric"
                   onChange={(e) => setCode(e.target.value)} placeholder="current code" type="text" value={code} />
            <button className="btn ghost" disabled={busy || !code.trim()} onClick={fresh}>New recovery codes</button>
            {status.required_by.length === 0 && (
              <button className="btn ghost" disabled={busy || !code.trim()} onClick={turnOff}>Turn it off</button>)}
          </div>
        </>
      )}
      <Err e={err} />
    </Section>
  );
}

function Sessions({ onSignedOut }) {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState(null);
  const load = useCallback(() => api.sessions().then(setRows).catch(setErr), []);
  useEffect(() => { load(); }, [load]);

  async function end(id) {
    setErr(null);
    try { await api.endSession(id); await load(); } catch (e) { setErr(e); }
  }
  async function everywhere() {
    setErr(null);
    try { await api.signOutEverywhere(); onSignedOut?.(); } catch (e) { setErr(e); }
  }

  return (
    <Section testid="security-sessions" title="Where you are signed in">
      <p className="sub">A session ends after 8 idle hours, and 12 hours after sign in whatever you do.</p>
      <div className="scroll"><table>
        <thead><tr><th>signed in</th><th>last seen</th><th>from</th><th>browser</th><th>org</th><th /></tr></thead>
        <tbody>
          {rows?.map((s) => (
            <tr key={s.id}>
              <td className="mono">{when(s.created_at)}</td>
              <td className="mono">{when(s.last_seen)}</td>
              <td className="mono">{s.ip || "unknown"}</td>
              <td>{(s.user_agent || "unknown").slice(0, 60)}</td>
              <td className="mono">{s.org_slug}</td>
              <td>{s.current ? <span className="badge ok">this one</span>
                : <button className="btn ghost" onClick={() => end(s.id)}>Sign out</button>}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      <button className="btn" onClick={everywhere} style={{ marginTop: 10 }}>Sign out everywhere</button>
      <Err e={err} />
    </Section>
  );
}

function Email({ me }) {
  const [sent, setSent] = useState(false);
  const [err, setErr] = useState(null);
  return (
    <Section testid="security-email" title="Email">
      <p className="sub"><span className="mono">{me.email}</span>{" "}
        {me.email_verified ? <span className="badge ok">confirmed</span> : <span className="badge hold">not confirmed yet</span>}
      </p>
      {!me.email_verified && (
        <button className="btn ghost" disabled={sent}
                onClick={() => api.resendVerify().then(() => setSent(true), setErr)}>
          {sent ? "Sent. Check your inbox" : "Send the link again"}</button>
      )}
      <Err e={err} />
    </Section>
  );
}

function Tokens({ slug }) {
  const [rows, setRows] = useState(null);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState(["read"]);
  const [made, setMade] = useState(null);
  const [err, setErr] = useState(null);
  const load = useCallback(() => api.tokens(slug).then(setRows).catch(setErr), [slug]);
  useEffect(() => { load(); }, [load]);

  async function create() {
    setErr(null);
    try { setMade(await api.createToken(slug, name, scopes)); setName(""); await load(); } catch (e) { setErr(e); }
  }
  async function revoke(id) {
    setErr(null);
    try { await api.revokeToken(slug, id); await load(); } catch (e) { setErr(e); }
  }
  const flip = (s) => setScopes(scopes.includes(s) ? scopes.filter((x) => x !== s) : [...scopes, s]);

  return (
    <Section testid="security-tokens" title="API tokens">
      <p className="sub">For scripts and services. A token speaks for the org, never for you, and it can
        never approve anything. I show it once.</p>
      {made && (
        <div data-testid="token-once" style={{ ...box, borderColor: "var(--accent)" }}>
          <b>{made.name}</b>
          <p className="note">Copy it now. It will not be shown again.</p>
          <pre className="mono" style={{ margin: 0, wordBreak: "break-all", whiteSpace: "pre-wrap" }}>{made.token}</pre>
          <button className="btn ghost" onClick={() => setMade(null)} style={{ marginTop: 10 }}>Done</button>
        </div>
      )}
      <div className="row">
        <input aria-label="token name" onChange={(e) => setName(e.target.value)} placeholder="name, e.g. nightly-ci"
               type="text" value={name} />
        {SCOPES.map(([s, label]) => (
          <label className="note" key={s} title={label}>
            <input checked={scopes.includes(s)} onChange={() => flip(s)} type="checkbox" /> {s}
          </label>
        ))}
        <button className="btn" disabled={!name.trim() || !scopes.length} onClick={create}>Make a token</button>
      </div>
      <div className="scroll"><table>
        <thead><tr><th>name</th><th>scopes</th><th>made</th><th>last used</th><th /></tr></thead>
        <tbody>
          {rows?.map((t) => (
            <tr key={t.id}>
              <td>{t.name}</td>
              <td className="mono">{t.scopes.join(" ")}</td>
              <td className="mono">{when(t.created_at)}</td>
              <td className="mono">{when(t.last_used)}</td>
              <td><button className="btn ghost" onClick={() => revoke(t.id)}>Revoke</button></td>
            </tr>
          ))}
          {rows?.length === 0 && <tr><td className="note" colSpan={5}>No tokens yet.</td></tr>}
        </tbody>
      </table></div>
      <Err e={err} />
    </Section>
  );
}

function RequireMfa({ slug }) {
  const [state, setState] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { api.orgSecurity(slug).then(setState).catch(setErr); }, [slug]);
  async function flip() {
    setErr(null);
    try { setState(await api.setOrgSecurity(slug, !state.require_mfa)); } catch (e) { setErr(e); }
  }
  return (
    <Section testid="security-org" title="Two-step sign in for the whole org">
      {state && (
        <>
          <p className="sub">{state.require_mfa ? "Required." : "Not required."} {state.members_with_mfa} of{" "}
            {state.members} people have it on. With it required, someone without it can only set it up.</p>
          <button className="btn" onClick={flip}>{state.require_mfa ? "Stop requiring it" : "Require it"}</button>
        </>
      )}
      <Err e={err} />
    </Section>
  );
}

function AuthAudit({ slug }) {
  const [got, setGot] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { api.authAudit(slug).then(setGot).catch(setErr); }, [slug]);
  return (
    <Section testid="security-audit" title="Auth audit">
      <p className="sub">Sign ins, failures, locks, two-step changes, tokens and people, in the org's hash chain.</p>
      <div className="row">
        {got && (
          <span className={`badge ${got.verified ? "ok" : "bad"}`} data-testid="auth-chain">
            {got.verified ? `chain intact · ${got.total} rows` : `CHAIN BROKEN at row ${got.broken_at}`}</span>
        )}
        <button className="btn ghost" onClick={() => api.exportAudit(slug).catch(setErr)}>Export the chain</button>
      </div>
      <div className="scroll"><table>
        <thead><tr><th>when</th><th>actor</th><th>event</th><th>detail</th></tr></thead>
        <tbody>
          {got?.rows.slice().reverse().map((r) => (
            <tr key={r.seq}>
              <td className="mono">{when(r.ts)}</td>
              <td className="mono">{r.actor}</td>
              <td className="mono">{r.action}</td>
              <td className="mono">{JSON.stringify(r.detail)}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      <Err e={err} />
    </Section>
  );
}

export default function Security({ me, onRotated, onSignedOut, required = false }) {
  if (required) return <TwoStep onRotated={onRotated} required />;
  const rank = RANK.indexOf(me.role);
  return (
    <>
      <Email me={me} />
      <TwoStep onRotated={onRotated} />
      <Sessions onSignedOut={onSignedOut} />
      {rank >= RANK.indexOf("owner") && <RequireMfa slug={me.org_slug} />}
      {rank >= RANK.indexOf("admin") && <Tokens slug={me.org_slug} />}
      {rank >= RANK.indexOf("admin") && <AuthAudit slug={me.org_slug} />}
    </>
  );
}
