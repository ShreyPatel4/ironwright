import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge, Err } from "../components.jsx";

export default function Ask({ ws }) {
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState(null);
  const [metricsData, setMetricsData] = useState(null);
  const [replayOut, setReplayOut] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [examples, setExamples] = useState([]);

  const loadMetrics = useCallback(() => {
    api.askMetrics(ws.name).then(setMetricsData).catch(() => setMetricsData(null));
  }, [ws.name]);
  useEffect(() => {
    setAnswer(null); setReplayOut(null); setErr(null); loadMetrics();
    api.askExamples(ws.name).then(setExamples).catch(() => setExamples([]));
  }, [loadMetrics, ws.name]);

  async function runText(text) {
    setQ(text); setBusy(true); setErr(null); setReplayOut(null);
    try { setAnswer(await api.ask(ws.name, text)); } catch (e) { setErr(e); setAnswer(null); }
    loadMetrics();
    setBusy(false);
  }

  async function run() {
    setBusy(true); setErr(null); setReplayOut(null);
    try { setAnswer(await api.ask(ws.name, q)); } catch (e) { setErr(e); setAnswer(null); }
    loadMetrics();
    setBusy(false);
  }

  async function replay() {
    setErr(null);
    try {
      // Replay the entry whose calls are on screen, not the log tail: another
      // ask (a second tab, an agent) may have appended since this answer.
      const log = await api.askLog(ws.name);
      const ids = new Set((answer?.calls ?? []).map((c) => c.call_id));
      let index = log.length - 1;
      for (let i = log.length - 1; i >= 0; i--) {
        if ((log[i].calls ?? []).some((c) => ids.has(c.call_id))) { index = i; break; }
      }
      setReplayOut(await api.askReplay(ws.name, index));
    } catch (e) { setErr(e); }
  }

  return (
    <>
      <h2>Ask</h2>
      <p className="sub">Questions route only to this workspace's governed functions; nothing is
        guessed. An answer carries the exact calls it is made of and can be replayed; a question
        outside the ontology is refused, with the reason, and the refusal rate is published below.</p>
      <div className="row">
        <input aria-label="your question" onChange={(e) => setQ(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && q.trim() && run()}
               placeholder="units and sales for item ITM-… in store STR-…"
               style={{ flex: 1, minWidth: 280 }} type="text" value={q} />
        <button className="btn" disabled={busy || !q.trim()} onClick={run}>{busy ? "Asking…" : "Ask"}</button>
      </div>
      {examples.length > 0 && (
        <div className="row">
          <span className="note">try one, built from real records your identity may read:</span>
          {examples.map((ex) => (
            <button className="btn ghost" key={ex.function} onClick={() => runText(ex.question)}
                    style={{ fontSize: 12 }}>
              {ex.question.length > 56 ? `${ex.question.slice(0, 55)}…` : ex.question}
            </button>
          ))}
        </div>
      )}
      {examples.length === 0 && !answer && (
        <p className="note">No example questions for this identity: policy permits it none of
          this workspace's functions. Change identity under GOVERN, Principals.</p>
      )}
      <Err e={err} />
      {answer && answer.status === "answered" && (
        <>
          <div aria-live="polite" className="row"><span className="badge ok">answered · {answer.narrator}</span>
            <Badge v={answer.verification} />
            <button className="btn ghost" onClick={replay}>Replay this answer</button>
            {replayOut && replayOut.replayable === false && (
              <span className="badge hold">not replayable: {replayOut.reason}</span>
            )}
            {replayOut && replayOut.replayable !== false && (
              <span className={`badge ${replayOut.match ? "ok" : "bad"}`}>
                {replayOut.match ? "replay matches, digest for digest" : "REPLAY DIVERGED"}
              </span>
            )}
          </div>
          <pre className="mono" style={{ whiteSpace: "pre-wrap" }}>{answer.answer}</pre>
          <h3>The calls this answer is made of</h3>
          <div className="scroll"><table>
            <thead><tr><th>call</th><th>function</th><th>args</th><th>result digest</th></tr></thead>
            <tbody>{answer.calls.map((c) => (
              <tr key={c.call_id}><td className="mono">{c.call_id}</td>
                <td className="mono">{c.function}</td>
                <td className="mono">{JSON.stringify(c.args)}</td>
                <td className="mono">{c.result_digest.slice(0, 16)}…</td></tr>))}
            </tbody>
          </table></div>
        </>
      )}
      {answer && answer.status === "refused" && (
        <div aria-live="polite" className="row">
          <span className="badge hold">refused · {answer.reason_code}</span>
          <span className="note">{answer.reason}</span>
        </div>
      )}
      {metricsData && metricsData.asked > 0 && (
        <p className="note" style={{ marginTop: 18 }}>
          This workspace so far: {metricsData.asked} asked, {metricsData.answered} answered,{" "}
          {metricsData.refused} refused (rate {metricsData.refusal_rate}). Refusals are a feature.
        </p>
      )}
    </>
  );
}
