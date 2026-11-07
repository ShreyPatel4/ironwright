import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { Err, ReadOnlyNote, Rows } from "../components.jsx";

// The bridge run board and its approval card. Every number comes from the server; the
// gate decides, and this screen only shows what it decided and carries a human's answer.

const GATE_DETAIL = {
  tests: (g) => (g.failed.length ? `failed: ${g.failed.join(", ")}` : `${g.ran} ran, none failed`),
  contract: (g) => `fit ${g.fit}${g.missing.length ? `, missing ${g.missing.join(", ")}` : ""}`,
  dq: (g) => `pass rate ${g.rate} against ${g.threshold}`,
  lineage: (g) => `coverage ${g.before} to ${g.after}`,
  cost: (g) => `${g.rows_out} rows out of ${g.rows_in} in, ratio ${g.ratio} against ${g.budget}`,
  compliance: (g) => [g.purpose ? `purpose: ${g.purpose}` : "no purpose declared",
    g.masked.length ? `masked ${g.masked.join(", ")}` : null,
    g.unmasked.length ? `unmasked ${g.unmasked.join(", ")}` : null].filter(Boolean).join(". "),
};

function Pass({ ok }) {
  return <span className={`badge ${ok ? "ok" : "bad"}`}>{ok ? "pass" : "fail"}</span>;
}

function Samples({ title, tables }) {
  return Object.entries(tables || {}).map(([name, rows]) => (
    <div key={`${title}-${name}`}>
      <p className="note">{title} · {name}</p>
      {rows.length ? <Rows columns={Object.keys(rows[0])} rows={rows.map((r) => Object.values(r))} />
        : <p className="note">no rows</p>}
    </div>
  ));
}

function Card({ ws, card, onDecided }) {
  const [reason, setReason] = useState("");
  const [err, setErr] = useState(null);
  const decided = card.decision;

  async function decide(decision) {
    setErr(null);
    try {
      onDecided(await api.bridgeDecide(ws.name, card.card_id,
        { decision, content_hash: card.content_hash, reason }));
    } catch (e) { setErr(e); }
  }

  return (
    <section aria-label="approval card">
      <h3>{card.kind === "stuck" ? "Stuck after three attempts" : "Promotion"} into {card.layer}</h3>
      <p className="note">
        <span className="mono">{card.card_id}</span> · {card.tables.join(", ")} · drafted by{" "}
        <span className="mono">{card.agent}</span> for <span className="mono">{card.requested_by}</span> ·
        hash <span className="mono">{card.content_hash.slice(0, 12)}</span>
      </p>
      {card.note && <p className="note">{card.note}</p>}

      <h3>Scores</h3>
      <div className="scroll"><table>
        <thead><tr><th>gate</th><th>result</th><th>detail</th><th>evidence</th></tr></thead>
        <tbody>
          {Object.entries(card.scores).map(([g, s]) => (
            <tr key={g}>
              <td>{g}</td><td><Pass ok={s.passed} /></td>
              <td>{GATE_DETAIL[g] ? GATE_DETAIL[g](s) : ""}</td>
              <td className="mono">{s.evidence}</td>
            </tr>
          ))}
        </tbody>
      </table></div>

      {card.attempts.length > 1 && (
        <>
          <h3>Attempts</h3>
          <Rows columns={["cycle", "attempt", "passed", "failed gates", "note"]}
                rows={card.attempts.map((a) => [a.cycle, a.attempt, a.passed ? "yes" : "no",
                  a.failed.join(", ") || "none", a.note])} />
        </>
      )}

      <h3>Diff</h3>
      {Object.keys(card.diff).length === 0 ? <p className="note">No change to the live models.</p> :
        Object.entries(card.diff).map(([f, d]) => <pre className="mono" key={f}>{d}</pre>)}

      <h3>Before and after, on the sample</h3>
      <p className="note">Columns tagged as personal data show as [masked] here, whatever the change does.</p>
      <Samples title="before" tables={card.samples.before} />
      <Samples title="after" tables={card.samples.after} />

      <h3>Evidence</h3>
      <p className="mono">{card.evidence.join("  ")}</p>

      <Err e={err} />
      {decided ? (
        <p aria-live="polite" className="note">
          {decided.decision === "approve" ? "Approved" : "Rejected"} by <span className="mono">{decided.by}</span>
          {decided.reason ? `: ${decided.reason}` : ""}
        </p>
      ) : !ws.read_only && (
        <div className="row">
          <label className="note" htmlFor="reject-reason">reason</label>
          <input id="reject-reason" onChange={(e) => setReason(e.target.value)}
                 placeholder="what the agent should change" style={{ minWidth: 260 }}
                 type="text" value={reason} />
          <button className="btn" disabled={!card.passed} onClick={() => decide("approve")}>Approve</button>
          <button className="btn danger" disabled={!reason.trim()} onClick={() => decide("reject")}>Reject</button>
          {!card.passed && <span className="note">A stuck change cannot be approved. Reject it with a reason.</span>}
        </div>
      )}
    </section>
  );
}

export default function RunBoard({ ws }) {
  const [board, setBoard] = useState(null);
  const [card, setCard] = useState(null);
  const [last, setLast] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(() => {
    api.bridgeBoard(ws.name).then(setBoard).catch(setErr);
  }, [ws.name]);
  useEffect(() => { setBoard(null); setCard(null); setLast(null); setErr(null); load(); }, [load]);

  async function open(id) {
    setErr(null);
    try { setCard(await api.bridgeCard(ws.name, id)); } catch (e) { setErr(e); }
  }

  async function startRun() {
    setErr(null); setBusy(true);
    try {
      const out = await api.bridgeRun(ws.name);
      setLast(`run ${out.run_id} is ${out.state}`);
      load();
      if (out.card_id) open(out.card_id);
    } catch (e) { setErr(e); } finally { setBusy(false); }
  }

  function decided(out) {
    setLast(`decision recorded, run ${out.run.run_id} is ${out.run.state}` +
      (out.run.refused ? `, refused: ${out.run.refused}` : ""));
    setCard(null);
    load();
  }

  const agents = board?.agents ?? [];
  const cell = (layer, agent) => board.cells.find((c) => c.layer === layer && c.agent === agent);

  return (
    <>
      <h2>Run board</h2>
      <p className="sub">Agents draft pipeline changes. I stage each one on a sample in a sandbox, score it
        with fixed gates, and send it back to the agent when it falls short, up to three times. Every change
        that passes, or gets stuck, comes here as one card. Nothing reaches a live model until a human who did
        not ask for it approves.</p>
      <Err e={err} />
      <ReadOnlyNote ws={ws} />
      <div className="row">
        {!ws.read_only && <button className="btn" disabled={busy} onClick={startRun}>
          {busy ? "Running" : "Draft silver from the silver draft"}</button>}
        <button className="btn ghost" onClick={load}>Refresh</button>
        {last && <span aria-live="polite" className="note">{last}</span>}
      </div>

      {board && (
        <>
          <h3>Layers by agent</h3>
          {agents.length === 0 ? <p className="note">No runs yet.</p> : (
            <div className="scroll"><table aria-label="run board">
              <thead><tr><th>layer</th>{agents.map((a) => <th className="mono" key={a}>{a}</th>)}</tr></thead>
              <tbody>
                {board.layers.map((l) => (
                  <tr key={l}>
                    <td>{l}</td>
                    {agents.map((a) => {
                      const c = cell(l, a);
                      return <td className="mono" key={a}>{c ? (
                        <>{c.runs} runs · {Object.entries(c.states).map(([s, n]) => `${n} ${s}`).join(", ")}
                          <br />{c.retries} retries · {c.escalations} escalations</>) : "·"}</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}

          <h3>Waiting on a human</h3>
          {board.queue.length === 0 ? <p className="note">Queue empty. Nothing is waiting.</p> : (
            <div className="scroll"><table aria-label="queue">
              <thead><tr><th>card</th><th>kind</th><th>tables</th><th>requested by</th><th></th></tr></thead>
              <tbody>
                {board.queue.map((q) => (
                  <tr key={q.card_id}>
                    <td className="mono">{q.card_id}</td><td>{q.kind}</td>
                    <td className="mono">{q.tables.join(", ")}</td><td className="mono">{q.requested_by}</td>
                    <td><button className="btn ghost" onClick={() => open(q.card_id)}>Open card</button></td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}

          {card && <Card card={card} onDecided={decided} ws={ws} />}

          {board.runs.length > 0 && (
            <>
              <h3>Runs</h3>
              <Rows columns={["run", "layer", "state", "attempt", "cycle", "evaluations"]}
                    rows={board.runs.map((r) => [r.run_id, r.layer, r.state, r.attempt, r.cycle,
                      r.evaluations.map((e) => (e.passed ? "pass" : `fail (${e.failed.join(", ")})`)).join(" → ")])} />
            </>
          )}
        </>
      )}
    </>
  );
}
