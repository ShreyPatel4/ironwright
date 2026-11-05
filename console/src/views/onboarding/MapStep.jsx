import { useEffect, useMemo, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";
import { canAdmin, Loading, Panel } from "./shared.jsx";

/* The proposed workspace.yaml, as a form, beside the silver draft. Contract 3 needs each
   column to carry the name the file has, because bronze holds the file as received. So a
   drop or a rename cannot touch the bytes: both land in a silver model, the one place the
   contract lets a column change its name or its type. The server drafted that model and
   the server writes it. All this form sends is what the human chose. */

const TYPES = ["string", "int", "bigint", "double", "boolean", "date", "timestamp"];
const IDENT = /^[a-z][a-z0-9_]{0,62}$/;
function toDraft(workspace) {
  return workspace.sources.map((s) => ({
    ...s,
    columns: s.columns.map((c) => ({ source: c.name, name: c.name, type: c.type,
                                     key: !!c.key, keep: true })),
  }));
}

export function renamesOf(draft) {
  const out = {};
  for (const s of draft) {
    const moved = s.columns.filter((c) => c.keep && c.name !== c.source);
    if (moved.length) out[s.name] = Object.fromEntries(moved.map((c) => [c.source, c.name]));
  }
  return out;
}

export function buildWorkspace(slug, uploadId, proposal, draft, declined) {
  const sources = draft.map(({ columns, ...source }) => ({
    ...source,
    columns: columns.filter((c) => c.keep).map((c) => ({
      name: c.source, type: c.type, ...(c.key ? { key: true } : {}),
    })),
  }));
  return {
    from_upload: uploadId,
    slug,
    spec: { ...proposal.workspace, sources },
    silver: { declined: [...declined].sort(), renames: renamesOf(draft) },
  };
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

function Decision({ decision: d, on, locked, onToggle }) {
  const label = `apply ${d.id}`;
  const lost = d.lost ? ` ${d.lost} become null.` : "";
  return (
    <div data-testid={`decision-${d.id}`} style={{ display: "flex", gap: 8, margin: "8px 0", opacity: on ? 1 : 0.5 }}>
      <input aria-label={label} checked={on} disabled={locked} onChange={onToggle} type="checkbox" />
      <span>
        {d.reason}
        <span className="note"> {d.rows} of {d.of} rows. {Math.round(d.confidence * 100)}% sure.{lost}</span>
      </span>
    </div>
  );
}

function SilverPanel({ source, declined, locked, onToggle }) {
  if (source.error) {
    return <Panel><b className="mono">silver.{source.name}</b><p className="note">{source.error}</p></Panel>;
  }
  return (
    <Panel>
      <b className="mono">silver.{source.name}</b>
      <span className="note"> {source.rows_out} rows come out of {source.rows_in}</span>
      {!source.key && <p className="note">{source.key_reason}</p>}
      {source.decisions.map((d) => (
        <Decision decision={d} key={d.id} locked={locked} on={!declined.has(d.id)} onToggle={() => onToggle(d.id)} />
      ))}
      <details>
        <summary className="note">The SQL, with every decision applied</summary>
        <pre className="mono" style={{ whiteSpace: "pre-wrap" }}>{source.sql}</pre>
      </details>
    </Panel>
  );
}

export default function MapStep({ journey, advance }) {
  const { upload, workspace, me } = journey;
  const [proposal, setProposal] = useState(null);
  const [draft, setDraft] = useState(null);
  const [silver, setSilver] = useState(null);
  const [declined, setDeclined] = useState(new Set());
  const [kept, setKept] = useState(null);
  const [rewritten, setRewritten] = useState(false);
  const [name, setName] = useState("");
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const locked = !!workspace || !canAdmin(me.role);
  const silverLocked = !canAdmin(me.role) || (!!workspace && !kept);

  useEffect(() => {
    if (!upload) return;
    api.uploadProposal(upload.id)
      .then((p) => { setProposal(p); setDraft(toDraft(p.workspace)); setSilver(p.silver ?? null); })
      .catch(setErr);
  }, [upload?.id]);

  // Once a workspace exists, its kept draft and choices are the truth, not the first draft.
  useEffect(() => {
    if (!workspace) return;
    api.silverDraft(workspace.name)
      .then((saved) => { setKept(saved); setSilver(saved.draft); setDeclined(new Set(saved.declined)); })
      .catch((e) => { if (e.status !== 404) setErr(e); });
  }, [workspace?.name]);

  function toggle(id) {
    setRewritten(false);
    const next = new Set(declined);
    if (next.has(id)) next.delete(id); else next.add(id);
    setDeclined(next);
  }

  async function rewrite() {
    setBusy(true); setErr(null); setRewritten(false);
    try {
      await api.rewriteSilver(workspace.name, [...declined].sort(), kept.renames);
      setRewritten(true);
    } catch (e) { setErr(e); }
    setBusy(false);
  }

  const wrong = useMemo(() => (draft ? problems(draft) : []), [draft]);

  async function create() {
    setBusy(true); setErr(null);
    try {
      await api.createTenantWorkspace(buildWorkspace(name, upload.id, proposal, draft, declined));
      await advance();
    } catch (e) { setErr(e); setBusy(false); }
  }

  const gaps = proposal?.decisions.filter((d) => d.contract_gap || d.value === "left out") ?? [];
  const silverOf = (name) => silver?.sources.find((s) => s.name === name);
  return (
    <>
      <h2>Map</h2>
      <p className="sub">This is what I propose to build from your files. Change anything: rename a
        column, change its type, mark the keys, drop what you do not need. Your file stays as it was,
        and these edits shape the silver tables. Beside each file is how I would clean it. Untick
        anything you do not want.</p>
      {!upload && <p className="note">There is nothing to map until you upload files.</p>}
      {upload && !proposal && !err && <Loading what="the proposal" />}
      {workspace && (
        <p className="note">You already made {workspace.name} from these files. The columns show my first
          draft, not your edits. The cleaning shows what you chose.</p>
      )}
      {!workspace && !canAdmin(me.role) && me.role !== "viewer" && (
        <p className="note">Creating a workspace needs an admin. You can read the draft.</p>
      )}
      {draft && (
        <>
          {draft.map((s, i) => (
            <div key={s.name} style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))" }}>
              <SourceForm locked={locked} source={s}
                          onChange={(columns) => setDraft(draft.map((x, j) => (j === i ? { ...x, columns } : x)))} />
              {silverOf(s.name) && (
                <SilverPanel declined={declined} locked={silverLocked} onToggle={toggle} source={silverOf(s.name)} />
              )}
            </div>
          ))}
          {workspace && kept && canAdmin(me.role) && (
            <div className="row">
              <button className="btn" disabled={busy} onClick={rewrite} type="button">
                {busy ? "Rewriting…" : "Rewrite the silver models"}
              </button>
              {rewritten && <span className="note">Done. The next run builds them.</span>}
            </div>
          )}
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
