import { useEffect, useState } from "react";
import { api } from "../../api.js";
import { Badge, Err } from "../../components.jsx";
import { canWrite, Panel } from "./shared.jsx";

/* The first question. An answer shows the evidence ids it stands on. A refusal
   shows its reason, because a refusal is the honest answer to a question the
   ontology cannot reach. */
export default function AskStep({ journey }) {
  const { workspace, me } = journey;
  const [question, setQuestion] = useState("");
  const [examples, setExamples] = useState(null);
  const [answer, setAnswer] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const writable = canWrite(me.role);

  useEffect(() => {
    if (!workspace) return;
    api.askExamples(workspace.name).then(setExamples).catch((e) => { setExamples([]); setErr(e); });
  }, [workspace?.name]);

  async function send(text) {
    setQuestion(text); setBusy(true); setErr(null); setAnswer(null);
    try { setAnswer(await api.ask(workspace.name, text)); } catch (e) { setErr(e); }
    setBusy(false);
  }

  return (
    <>
      <h2>Ask</h2>
      <p className="sub">Ask about your own data. Every answer cites the evidence it comes from, and I
        refuse anything your ontology cannot reach.</p>
      {!workspace && <p className="note">There is nothing to ask until the ontology is accepted.</p>}
      {workspace && (
        <>
          <div className="row">
            <input aria-label="your question" disabled={!writable}
                   onChange={(e) => setQuestion(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && question.trim() && !busy && send(question)}
                   placeholder="Name a real id from your data"
                   style={{ flex: 1, minWidth: 280 }} type="text" value={question} />
            <button className="btn" disabled={!writable || busy || !question.trim()}
                    onClick={() => send(question)} type="button">{busy ? "Asking…" : "Ask"}</button>
          </div>
          {examples === null && <p className="note">Loading example questions.</p>}
          {examples?.length === 0 && !err && <p className="note">I have no example questions for your role.</p>}
          {examples?.length > 0 && (
            <div aria-label="example questions" className="row" role="group">
              <span className="note">try one, built from your real records:</span>
              {examples.map((ex) => (
                <button className="btn ghost" disabled={!writable || busy} key={ex.question}
                        onClick={() => send(ex.question)} style={{ fontSize: 12 }} type="button">
                  {ex.question}
                </button>
              ))}
            </div>
          )}
        </>
      )}
      <Err e={err} />
      {answer?.status === "refused" && (
        <Panel>
          <div aria-live="polite" className="row" style={{ margin: 0 }}>
            <span className="badge hold">refused · {answer.reason_code}</span>
            <span className="note">{answer.reason}</span>
          </div>
        </Panel>
      )}
      {answer?.status === "answered" && (
        <Panel>
          <div aria-live="polite" className="row" style={{ margin: "0 0 8px" }}>
            <span className="badge ok">answered · {answer.narrator}</span>
            <Badge v={answer.verification} />
          </div>
          <pre aria-label="answer" className="mono" style={{ margin: 0, whiteSpace: "pre-wrap" }}>{answer.answer}</pre>
          <h3>Evidence</h3>
          {answer.citations.length === 0 && <p className="note">This answer cites no evidence.</p>}
          <div className="row" style={{ margin: 0 }}>
            {answer.citations.map((id) => (
              <span aria-label={`evidence ${id}`} className="mono" key={id}
                    style={{ border: "1px solid var(--rule)", borderRadius: "var(--rad)", padding: "2px 8px" }}>
                {id}
              </span>
            ))}
          </div>
        </Panel>
      )}
    </>
  );
}
