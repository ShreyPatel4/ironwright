import { useEffect, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";
import { canAdmin, canWrite, JobLog, JobState, Loading, Panel, runPassed, useJob } from "./shared.jsx";

/* W2's proposal, for a human to accept or edit. Rules propose, a person decides.
   Only what changed goes in the edits, so the server can tell a choice from a default.
   The server applies them in order and by the current name, and a dropped object takes
   its links with it. So link drops go first and a rename goes last. */

export function buildEdits(proposal, objects, links) {
  const dropped = (name) => !objects[name].keep;
  return [
    ...proposal.links
      .filter((l) => links[l.name] === false && !dropped(l.from) && !dropped(l.to))
      .map((l) => ({ op: "drop_link", link: l.name })),
    ...proposal.objects.flatMap((o) => {
      const e = objects[o.name];
      if (!e.keep) return [{ op: "drop_object", object: o.name }];
      return [
        ...(e.key !== o.key ? [{ op: "set_key", object: o.name, key: e.key }] : []),
        ...(e.name !== o.name ? [{ op: "rename_object", object: o.name, to: e.name }] : []),
      ];
    }),
  ];
}

function ObjectForm({ object, edit, locked, onChange }) {
  const set = (patch) => onChange({ ...edit, ...patch });
  return (
    <Panel style={{ opacity: edit.keep ? 1 : 0.5 }}>
      <div className="row" style={{ margin: 0 }}>
        <label className="note">
          <input aria-label={`keep object ${object.name}`} checked={edit.keep} disabled={locked}
                 onChange={(e) => set({ keep: e.target.checked })} type="checkbox" /> keep
        </label>
        <input aria-label={`object name ${object.name}`} disabled={locked || !edit.keep}
               onChange={(e) => set({ name: e.target.value })} type="text" value={edit.name} />
        <span className="note">key</span>
        <select aria-label={`key of ${object.name}`} disabled={locked || !edit.keep}
                onChange={(e) => set({ key: e.target.value })} value={edit.key}>
          {object.properties.map((p) => (
            <option disabled={!p.unique} key={p.name} value={p.name}>{p.name}</option>
          ))}
        </select>
        <span className="note mono">{object.table} · confidence {object.confidence}</span>
      </div>
      <p className="note" style={{ margin: "8px 0 4px" }}>{object.reason}</p>
      <p className="mono" style={{ margin: 0 }}>{object.properties.map((p) => p.name).join(", ")}</p>
    </Panel>
  );
}

/* Accept queues the run that builds the objects. Ask has nothing to read until it ends. */
function Building({ jobId, advance }) {
  const { job, log, err } = useJob(jobId);
  const passed = runPassed(job);
  useEffect(() => { if (passed) advance(); }, [passed, advance]);
  return (
    <Panel>
      <p style={{ margin: "0 0 6px" }}>Accepted. I am building the objects now.</p>
      <JobState job={job} />
      {job?.error && <Err e={job.error} />}
      <JobLog text={log} />
      <Err e={err} />
    </Panel>
  );
}

function Review({ workspace, proposal, locked, advance }) {
  const [objects, setObjects] = useState(() => Object.fromEntries(proposal.objects.map((o) => (
    [o.name, { keep: true, name: o.name, key: o.key }]
  ))));
  const [links, setLinks] = useState({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [building, setBuilding] = useState(null);

  async function accept() {
    setBusy(true); setErr(null);
    try {
      const done = await api.ontologyAccept(workspace.name, proposal.id, buildEdits(proposal, objects, links));
      setBuilding(done.job_id);
    } catch (e) { setErr(e); setBusy(false); }
  }

  if (building) return <Building advance={advance} jobId={building} />;
  return (
    <>
      {proposal.objects.length === 0 && <p className="note">The proposal has no objects in it.</p>}
      {(proposal.skipped ?? []).map((t) => (
        <Panel key={t.table}>
          <b className="mono">{t.table}</b> <span className="note">became no object. {t.reason}</span>
        </Panel>
      ))}
      {proposal.objects.map((o) => (
        <ObjectForm edit={objects[o.name]} key={o.name} locked={locked} object={o}
                    onChange={(edit) => setObjects({ ...objects, [o.name]: edit })} />
      ))}
      <h3>Links</h3>
      {proposal.links.length === 0 && <p className="note">The proposal has no links in it.</p>}
      {proposal.links.map((l) => (
        <Panel key={l.name}>
          <label>
            <input aria-label={`keep link ${l.name}`} checked={links[l.name] !== false} disabled={locked}
                   onChange={(e) => setLinks({ ...links, [l.name]: e.target.checked })} type="checkbox" />
            {" "}<span className="mono">{l.from} to {l.to} via {l.via}</span>
          </label>
          <span className="note mono"> {l.cardinality} · confidence {l.confidence}</span>
          <p className="note" style={{ margin: "6px 0 0" }}>{l.reason}</p>
        </Panel>
      ))}
      {!locked && proposal.objects.length > 0 && (
        <button className="btn" disabled={busy} onClick={accept} type="button">
          {busy ? "Accepting…" : "Accept this ontology"}
        </button>
      )}
      <Err e={err} />
    </>
  );
}

export default function OntologyStep({ journey, advance }) {
  const { workspace, proposeJob, ontologyAccepted, me } = journey;
  const [startedId, setStartedId] = useState(null);
  const jobId = startedId ?? proposeJob?.id ?? null;
  const { job, log, err: pollErr } = useJob(jobId);
  const [proposal, setProposal] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const state = job?.state;

  useEffect(() => {
    if (state !== "succeeded") return;
    api.ontologyProposal(workspace.name).then(setProposal).catch(setErr);
  }, [state, workspace?.name]);

  async function start() {
    setBusy(true); setErr(null);
    try { setStartedId((await api.createJob("ontology_propose", workspace.name)).id); }
    catch (e) { setErr(e); }
    setBusy(false);
  }

  const locked = ontologyAccepted || !canAdmin(me.role);
  return (
    <>
      <h2>Ontology</h2>
      <p className="sub">The objects and links I found in your tables, each with its reason. Nothing
        goes live until a person accepts it. Then Ask can answer from it.</p>
      {!workspace && <p className="note">There is nothing to propose from until a run has passed.</p>}
      {ontologyAccepted && <p className="note">This ontology is accepted. It is shown here as proposed.</p>}
      {workspace && !ontologyAccepted && !canAdmin(me.role) && me.role !== "viewer" && (
        <p className="note">Accepting an ontology needs an admin. You can read the proposal.</p>
      )}
      {workspace && !jobId && !canWrite(me.role) && <p className="note">Nobody has asked for a proposal yet.</p>}
      {workspace && !jobId && canWrite(me.role) && (
        <button className="btn" disabled={busy} onClick={start} type="button">Propose an ontology</button>
      )}
      {jobId && state !== "succeeded" && (
        <Panel>
          <JobState job={job} />
          {job?.error && <Err e={job.error} />}
          <JobLog text={log} />
          {(state === "failed" || state === "cancelled") && canWrite(me.role) && (
            <button className="btn" disabled={busy} onClick={start} style={{ marginTop: 10 }} type="button">
              Propose again
            </button>
          )}
        </Panel>
      )}
      <Err e={err || pollErr} />
      {state === "succeeded" && !proposal && !err && <Loading what="the proposal" />}
      {proposal && <Review advance={advance} locked={locked} proposal={proposal} workspace={workspace} />}
      {ontologyAccepted && <button className="btn" onClick={() => advance()} style={{ marginTop: 10 }} type="button">Continue to Ask</button>}
    </>
  );
}
