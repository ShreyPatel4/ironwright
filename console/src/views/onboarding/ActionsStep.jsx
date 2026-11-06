import { useCallback, useEffect, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";
import { canAdmin, Loading, Panel } from "./shared.jsx";

/* Nothing is callable until a person writes a permit. I pick a template, an object
   type, who may ask and whether a second person approves. The server writes the rule
   and says it in plain words. Accept stores it. This step is optional: Ask works
   without it. */
export default function ActionsStep({ journey, advance }) {
  const { workspace, me } = journey;
  const admin = canAdmin(me.role);
  const [catalog, setCatalog] = useState(null);
  const [choices, setChoices] = useState({ template: "", object_type: "", request_role: "member",
                                           requires_approval: true });
  const [values, setValues] = useState("");
  const [draft, setDraft] = useState(null);
  const [done, setDone] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(() => {
    if (!workspace) return;
    api.actions(workspace.name).then((c) => {
      setCatalog(c);
      setChoices((ch) => ({ ...ch, template: ch.template || c.templates[0]?.name || "",
                            object_type: ch.object_type || c.object_types[0]?.name || "" }));
    }).catch(setErr);
  }, [workspace?.name]);
  useEffect(() => { load(); }, [load]);

  const template = catalog?.templates.find((t) => t.name === choices.template);
  const otype = catalog?.object_types.find((t) => t.name === choices.object_type);
  const fits = !template?.needs || otype?.properties.includes(template.needs);
  const body = () => ({ ...choices, ...(template?.picks_values
    ? { values: values.split(",").map((v) => v.trim()).filter(Boolean) } : {}) });

  function pick(patch) { setChoices({ ...choices, ...patch }); setDraft(null); setDone(null); }

  async function preview() {
    setBusy(true); setErr(null); setDraft(null);
    try { setDraft(await api.actionPropose(workspace.name, body())); } catch (e) { setErr(e); }
    setBusy(false);
  }

  async function accept() {
    setBusy(true); setErr(null);
    try { setDone(await api.actionAccept(workspace.name, body(), draft.id)); setDraft(null); load(); }
    catch (e) { setErr(e); }
    setBusy(false);
  }

  return (
    <>
      <h2>Actions</h2>
      <p className="sub">An action changes one object, and only after a person you trust says yes.
        Nothing here is callable until you accept a permit. Every request and every decision
        lands in the audit log.</p>
      {!catalog && !err && <Loading what="the action templates" />}
      {!admin && <p className="note">Only an owner or admin can write a permit. You can see what is allowed.</p>}
      {catalog && (
        <Panel>
          <div className="row" style={{ margin: 0 }}>
            <label className="note">template{" "}
              <select aria-label="template" disabled={!admin} value={choices.template}
                      onChange={(e) => pick({ template: e.target.value })}>
                {catalog.templates.map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
              </select>
            </label>
            <label className="note">on{" "}
              <select aria-label="object type" disabled={!admin} value={choices.object_type}
                      onChange={(e) => pick({ object_type: e.target.value })}>
                {catalog.object_types.map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
              </select>
            </label>
            <label className="note">who may ask{" "}
              <select aria-label="who may ask" disabled={!admin} value={choices.request_role}
                      onChange={(e) => pick({ request_role: e.target.value })}>
                <option value="member">members and up</option>
                <option value="admin">admins and owners</option>
                <option value="owner">owners only</option>
              </select>
            </label>
            <label className="note">
              <input aria-label="requires approval" checked={choices.requires_approval} disabled={!admin}
                     onChange={(e) => pick({ requires_approval: e.target.checked })} type="checkbox" />{" "}
              a second person approves
            </label>
          </div>
          {template && <p className="note" style={{ margin: "8px 0 0" }}>{template.description}</p>}
          {template?.picks_values && (
            <div className="row">
              <label className="note" htmlFor="action-values">statuses it may set, comma separated</label>
              <input disabled={!admin} id="action-values" onChange={(e) => { setValues(e.target.value); setDraft(null); }}
                     placeholder="open, closed" type="text" value={values} />
            </div>
          )}
          {!fits && <p className="note">{choices.object_type} has no {template.needs} property, so this template
            does not apply to it.</p>}
          <div className="row">
            <button className="btn ghost" disabled={!admin || busy || !fits} onClick={preview} type="button">
              Preview the rule
            </button>
          </div>
        </Panel>
      )}
      {draft && (
        <Panel>
          <h3 style={{ marginTop: 0 }}>{draft.name}</h3>
          <p aria-label="the rule in plain words" style={{ margin: "0 0 8px" }}>{draft.plain}</p>
          <pre className="mono" style={{ margin: 0, whiteSpace: "pre-wrap" }}>
            {JSON.stringify(draft.policy_rule, null, 1)}
          </pre>
          <div className="row">
            <button className="btn" disabled={busy} onClick={accept} type="button">Accept this permit</button>
          </div>
        </Panel>
      )}
      {done && <p aria-live="polite" className="note">Accepted <b>{done.action}</b>, audit row {done.audit_seq}.
        It shows as a button on each {choices.object_type} page and in Ask.</p>}
      <Err e={err} />
      {catalog?.accepted.length > 0 && (
        <>
          <h3>Accepted</h3>
          {catalog.accepted.map((a) => (
            <Panel key={a.name}>
              <p className="mono" style={{ margin: 0 }}>{a.name}</p>
              <p className="note" style={{ margin: "4px 0 0" }}>{a.plain}</p>
            </Panel>
          ))}
        </>
      )}
      <div className="row">
        <button className="btn ghost" onClick={() => advance("ask")} type="button">On to Ask</button>
      </div>
    </>
  );
}
