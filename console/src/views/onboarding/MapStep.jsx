import { useEffect, useMemo, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";
import { canAdmin, Loading, Panel } from "./shared.jsx";

/* The proposed workspace.yaml, as a form. Contract 3 needs each column to carry the
   name the file has, because bronze holds the file as received. So a drop or a rename
   cannot touch the bytes: both land in a silver model, the one place the contract
   lets a column change its name or its type. */

const TYPES = ["string", "int", "bigint", "double", "boolean", "date", "timestamp"];
const IDENT = /^[a-z][a-z0-9_]{0,62}$/;
const quoted = (name) => `"${name}"`;

function toDraft(workspace) {
  return workspace.sources.map((s) => ({
    ...s,
    columns: s.columns.map((c) => ({ source: c.name, name: c.name, type: c.type,
                                     key: !!c.key, keep: true })),
  }));
}

export function silverSql(source) {
  const select = source.columns.filter((c) => c.keep).map((c) => (
    c.type === "string" || c.type === "varchar"
      ? `${quoted(c.source)} as ${quoted(c.name)}`
      : `try_cast(${quoted(c.source)} as ${c.type}) as ${quoted(c.name)}`
  ));
  return `select ${select.join(",\n       ")}\nfrom bronze_${source.name}\n`;
}

export function buildWorkspace(name, proposal, draft) {
  const sources = draft.map(({ columns, ...source }) => ({
    ...source,
    columns: columns.filter((c) => c.keep).map((c) => ({
      name: c.source, type: c.type, ...(c.key ? { key: true } : {}),
    })),
  }));
  const silver = Object.fromEntries(draft.map((s, i) => (
    [`${String(i + 1).padStart(2, "0")}_${s.name}.sql`, silverSql(s)]
  )));
  return { name, spec: { ...proposal.workspace, sources }, models: { silver } };
}

export function problems(draft) {
  const out = [];
  for (const s of draft) {
    const kept = s.columns.filter((c) => c.keep);
    if (kept.length === 0) out.push(`${s.name} needs at least one column.`);
    for (const c of kept) {
      if (!IDENT.test(c.name)) {
        out.push(`${c.name || "An empty name"} is not a usable column name. Use lowercase letters, digits and underscores.`);
      }
    }
    const names = kept.map((c) => c.name);
    for (const n of new Set(names.filter((x, i) => names.indexOf(x) !== i))) {
      out.push(`${s.name} has two columns named ${n}.`);
    }
  }
  return out;
}

function SourceForm({ source, locked, onChange }) {
  const set = (i, patch) => onChange(source.columns.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const options = (c) => [...new Set([...TYPES, c.type])];
  return (
    <Panel>
      <b className="mono">{source.name}</b>
      <span className="note"> from {source.upload.split("/").at(-1)}</span>
      <div className="scroll" style={{ marginTop: 10 }}>
        <table>
          <thead><tr><th>keep</th><th>column in the file</th><th>name</th><th>type</th><th>key</th></tr></thead>
          <tbody>
            {source.columns.map((c, i) => {
              const at = `${source.name}.${c.source}`;
              return (
                <tr key={c.source} style={{ opacity: c.keep ? 1 : 0.5 }}>
                  <td><input aria-label={`keep ${at}`} checked={c.keep} disabled={locked}
                             onChange={(e) => set(i, { keep: e.target.checked })} type="checkbox" /></td>
                  <td className="mono">{c.source}</td>
                  <td><input aria-label={`rename ${at}`} disabled={locked || !c.keep}
                             onChange={(e) => set(i, { name: e.target.value })}
                             style={{ width: 170 }} type="text" value={c.name} /></td>
                  <td><select aria-label={`type ${at}`} disabled={locked || !c.keep}
                              onChange={(e) => set(i, { type: e.target.value })} value={c.type}>
                    {options(c).map((t) => <option key={t} value={t}>{t}</option>)}
                  </select></td>
                  <td><input aria-label={`key ${at}`} checked={c.key} disabled={locked || !c.keep}
                             onChange={(e) => set(i, { key: e.target.checked })} type="checkbox" /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

export default function MapStep({ journey, advance }) {
  const { upload, workspace, me } = journey;
  const [proposal, setProposal] = useState(null);
  const [draft, setDraft] = useState(null);
  const [name, setName] = useState("");
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const locked = !!workspace || !canAdmin(me.role);

  useEffect(() => {
    if (!upload) return;
    api.uploadProposal(upload.id)
      .then((p) => { setProposal(p); setDraft(toDraft(p.workspace)); })
      .catch(setErr);
  }, [upload?.id]);

  const wrong = useMemo(() => (draft ? problems(draft) : []), [draft]);

  async function create() {
    setBusy(true); setErr(null);
    try {
      await api.createTenantWorkspace(buildWorkspace(name, proposal, draft));
      await advance();
    } catch (e) { setErr(e); setBusy(false); }
  }

  const gaps = proposal?.decisions.filter((d) => d.contract_gap || d.value === "left out") ?? [];
  return (
    <>
      <h2>Map</h2>
      <p className="sub">This is what I propose to build from your files. Change anything: rename a
        column, change its type, mark the keys, drop what you do not need. Your file stays as it was,
        and these edits shape the silver tables.</p>
      {!upload && <p className="note">There is nothing to map until you upload files.</p>}
      {upload && !proposal && !err && <Loading what="the proposal" />}
      {workspace && (
        <p className="note">You already made {workspace.name} from these files. This is my first draft, not your edits.</p>
      )}
      {!workspace && !canAdmin(me.role) && me.role !== "viewer" && (
        <p className="note">Creating a workspace needs an admin. You can read the draft.</p>
      )}
      {draft && (
        <>
          {draft.map((s, i) => (
            <SourceForm key={s.name} locked={locked} source={s}
                        onChange={(columns) => setDraft(draft.map((x, j) => (j === i ? { ...x, columns } : x)))} />
          ))}
          {gaps.length > 0 && (
            <Panel>
              <b>What the draft could not say</b>
              {gaps.map((d) => (
                <p className="note" key={d.path} style={{ margin: "4px 0" }}>
                  <span className="mono">{d.path}</span> {d.reason}
                </p>
              ))}
            </Panel>
          )}
          {wrong.map((w) => <div className="err" key={w}>{w}</div>)}
          {!locked && (
            <div className="row">
              <span>
                <label className="note" htmlFor="ws-name">workspace name</label><br />
                <input id="ws-name" onChange={(e) => setName(e.target.value)}
                       placeholder="lowercase, like acme-claims" style={{ width: 240 }}
                       type="text" value={name} />
              </span>
              <button className="btn" disabled={busy || !name || wrong.length > 0}
                      onClick={create} style={{ alignSelf: "flex-end" }} type="button">
                {busy ? "Creating…" : "Create the workspace"}
              </button>
            </div>
          )}
        </>
      )}
      <Err e={err} />
    </>
  );
}
