import { useCallback, useEffect, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";

const PROVIDERS = ["anthropic", "openai"];

/* The server stores the key sealed and only ever answers with its last 4. This
   screen drops what you typed as soon as the server has it. */
export default function ModelKey({ slug }) {
  const [held, setHeld] = useState(null);
  const [provider, setProvider] = useState(PROVIDERS[0]);
  const [key, setKey] = useState("");
  const [err, setErr] = useState(null);

  const load = useCallback(() => api.llmKey(slug).then(setHeld).catch(setErr), [slug]);
  useEffect(() => { load(); }, [load]);

  async function save(e) {
    e.preventDefault();
    setErr(null);
    try { setHeld(await api.setLlmKey(slug, provider, key)); setKey(""); }
    catch (e2) { setErr(e2); }
  }

  async function remove() {
    setErr(null);
    try { await api.removeLlmKey(slug); await load(); }
    catch (e2) { setErr(e2); }
  }

  return (
    <>
      <p className="sub">With a key of your own, this org's questions go to your provider on your bill,
        and never use the platform's key.</p>
      {held?.provider
        ? (
          <div className="row">
            <span className="badge ok">{held.provider} key ending in {held.last4}</span>
            <button className="btn danger" onClick={remove}>Remove key</button>
          </div>
        )
        : held && <p className="note">No key saved.</p>}
      <form className="row" onSubmit={save}>
        <select aria-label="Provider" onChange={(e) => setProvider(e.target.value)} value={provider}>
          {PROVIDERS.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <input aria-label="API key" autoComplete="off" onChange={(e) => setKey(e.target.value)}
               style={{ width: 340 }} type="password" value={key} />
        <button className="btn" disabled={!key} type="submit">Save key</button>
      </form>
      <Err e={err} />
    </>
  );
}
