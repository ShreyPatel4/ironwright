import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

const VERDICT_CLASS = { approved: "ok", denied: "bad", denied_by_policy: "hold" };

const PLACEHOLDER = '[\n  {"id": "trial-rule", "effect": "permit", "principal": "user:…", "action": "action:…"}\n]';

export default function Training({ ws }) {
  const [labels, setLabels] = useState([]);
  const [rulesText, setRulesText] = useState("");
  const [report, setReport] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(() => {
    api.trainingLabels(ws.name).then((r) => setLabels(r.labels || [])).catch(setErr);
  }, [ws.name]);
  useEffect(() => { setReport(null); setErr(null); load(); }, [load]);

  async function evaluate() {
    setErr(null);
    setReport(null);
    let rules;
    try {
      rules = JSON.parse(rulesText || "[]");
    } catch (e) {
      setErr(new Error(`the proposal must be valid JSON: ${e.message}`));
      return;
    }
    if (!Array.isArray(rules)) {
      setErr(new Error("the proposal must be a JSON array of rules"));
      return;
    }
    try { setReport(await api.trainingEvaluate(ws.name, rules)); } catch (e) { setErr(e); }
  }

  return (
    <>
      <h2>Training</h2>
      <p className="sub">Nothing retrains silently. Every label below is an audit row replayed:
        a human approval, a human denial, or a straight policy denial. Evaluate scores a
        proposed rule change against those verdicts, offline. Adoption only happens through
        the reviewed governance paths, org policy apply and escalation grants; no model is
        fine-tuned here.</p>
      <div className="row">
        <button className="btn ghost" onClick={load}>Refresh</button>
        <span className="note">{labels.length} labels, derived from the hash-chained audit log</span>
      </div>
      <Err e={err} />
      {labels.length === 0 ? <p className="note">No labels yet. Approvals, denials and policy
        denials become labels once they land in the audit log.</p> : (
        <div className="scroll"><table>
          <thead><tr><th>seq</th><th>verdict</th><th>action</th><th>requested by</th>
            <th>decided by</th><th>rule</th><th>params</th></tr></thead>
          <tbody>
            {labels.map((l) => (
              <tr key={l.seq}>
                <td className="mono">{l.seq}</td>
                <td><span className={`badge ${VERDICT_CLASS[l.verdict] || ""}`}>{l.verdict}</span></td>
                <td className="mono">{l.action}</td>
                <td className="mono">{l.requested_by}</td>
                <td className="mono">{l.decided_by || "policy"}</td>
                <td className="mono">{l.rule || ""}</td>
                <td className="mono">{JSON.stringify(l.params)}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}

      <h3>Evaluate a proposal</h3>
      <p className="note">Paste candidate rules in the policies.yaml rule schema, as a JSON array.
        The evaluation replays every label through the pack rules plus your proposal and reports
        agreement with the human verdicts. It adopts nothing.</p>
      <textarea onChange={(e) => setRulesText(e.target.value)} placeholder={PLACEHOLDER}
                spellCheck={false} value={rulesText} />
      <div className="row">
        <button className="btn" onClick={evaluate}>Evaluate</button>
      </div>
      {report && (
        <>
          <p aria-live="polite" className="note">
            agreement <b>{report.agreement_rate === null ? "n/a" : report.agreement_rate}</b>
            {" "}({report.agreements} of {report.labels} labels) · adopted:{" "}
            <b>{String(report.adopted)}</b>
          </p>
          <p className="note">{report.note}</p>
          {report.disagreements.length === 0 ? (
            <p className="note">No disagreements: the proposed rules match every prior human verdict.</p>
          ) : (
            <div className="scroll"><table>
              <thead><tr><th>seq</th><th>action</th><th>human said</th><th>engine says</th></tr></thead>
              <tbody>
                {report.disagreements.map((d) => (
                  <tr key={d.seq}>
                    <td className="mono">{d.seq}</td>
                    <td className="mono">{d.action}</td>
                    <td className="mono">{d.expected}</td>
                    <td className="mono">{d.got}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
        </>
      )}
    </>
  );
}
