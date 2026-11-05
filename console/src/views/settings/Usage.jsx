import { useEffect, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";
import { Loading } from "../onboarding/shared.jsx";

const LABELS = [
  ["runs", "Runs today"],
  ["uploads", "Uploads today"],
  ["ask", "Ask questions today"],
  ["llm_tokens", "Model tokens today"],
  ["storage_bytes", "Storage in bytes"],
  ["members", "Members and open invites"],
];

export default function Usage({ slug }) {
  const [got, setGot] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => { api.usage(slug).then(setGot).catch(setErr); }, [slug]);

  if (!got) return err ? <Err e={err} /> : <Loading what="your usage" />;
  return (
    <>
      <p className="sub">Your plan is <b>{got.plan}</b>. Daily counts start again at 00:00 UTC,
        in {Math.ceil(got.resets_in_seconds / 3600)} hours or less.</p>
      <div className="scroll"><table>
        <thead><tr><th>what</th><th>used</th><th>limit</th></tr></thead>
        <tbody>
          {LABELS.map(([k, label]) => (
            <tr key={k}>
              <td>{label}</td>
              <td className="mono">{got.metrics[k].used}</td>
              <td className="mono">{got.metrics[k].limit ?? "no limit"}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      <p className="note">Tokens spent on your own model key today: {got.byo_llm_tokens}. They do not count against the limit.</p>
    </>
  );
}
