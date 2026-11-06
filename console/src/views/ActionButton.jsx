import { useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

/* One governed action on one object. The server decides everything: who may ask,
   whether a second person approves, and whether the object is in a state it acts on.
   This only collects the fields the action takes and shows what the server said. */
export default function ActionButton({ ws, action, label, fixed, fields = {}, requiresApproval }) {
  const [values, setValues] = useState(() => Object.fromEntries(Object.entries(fields).map(([k, spec]) => (
    [k, spec?.values?.[0] ?? ""]))));
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState(null);
  const [err, setErr] = useState(null);
  const missing = Object.entries(values).some(([, v]) => !String(v).trim());

  async function go() {
    setBusy(true); setErr(null); setOut(null);
    try { setOut(await api.act(ws, action, { ...fixed, ...values })); } catch (e) { setErr(e); }
    setBusy(false);
  }

  return (
    <span className="row" style={{ display: "inline-flex", margin: "0 10px 6px 0" }}>
      {Object.entries(fields).map(([name, spec]) => (spec?.values ? (
        <select aria-label={`${action} ${name}`} key={name} value={values[name]}
                onChange={(e) => setValues({ ...values, [name]: e.target.value })}>
          {spec.values.map((v) => <option key={v} value={v}>{v}</option>)}
        </select>
      ) : (
        <input aria-label={`${action} ${name}`} key={name} placeholder={name} type="text" value={values[name]}
               maxLength={spec?.max_length} onChange={(e) => setValues({ ...values, [name]: e.target.value })} />
      )))}
      <button className="btn" disabled={busy || missing} onClick={go} title={label} type="button">
        {requiresApproval ? `Ask to ${action}` : action}
      </button>
      {out && (
        <span aria-live="polite" className={`badge ${out.status === "needs_approval" ? "hold"
          : out.status?.startsWith("applied") ? "ok" : "bad"}`}>
          {out.status === "needs_approval" ? "waiting for an approver" : out.status}
          {out.status !== "needs_approval" && !out.status?.startsWith("applied") ? ` · ${out.reason}` : ""}
        </span>
      )}
      <Err e={err} />
    </span>
  );
}
