import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Err } from "../components.jsx";

const CLASSES = ["all", "identity", "contradiction", "orphan"];

export default function Discovery({ ws }) {
  const [data, setData] = useState(null);
  const [cls, setCls] = useState("all");
  const [open, setOpen] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    setData(null); setOpen(null); setErr(null); setCls("all");
    api.discovery(ws.name).then(setData).catch(setErr);
  }, [ws.name]);

  if (err) return <><h2>Discovery</h2><Err e={err} /></>;
  if (!data) return <><h2>Discovery</h2><p className="note">Deriving…</p></>;

  const rows = data.findings.filter((f) => cls === "all" || f.class === cls);

  return (
    <>
      <h2>Discovery</h2>
      <p className="sub">What this estate did not know about itself: identities living as
        several records, numbers that disagree between old and new, checks that fail and loads
        that never published. {data.note}</p>
      <div className="row">
        {CLASSES.map((c) => (
          <button className="btn ghost" key={c} onClick={() => setCls(c)}
                  style={c === cls ? { borderColor: "var(--accent)", color: "var(--accent)" } : {}}>
            {c}{c !== "all" && data.counts[c] ? ` ${data.counts[c]}` : ""}
          </button>
        ))}
      </div>
      {rows.length === 0 && <p className="note">Nothing found in this class. An empty class
        stays empty; Discovery does not invent findings.</p>}
      <div className="scroll"><table>
        <thead><tr><th>severity</th><th>class</th><th>finding</th><th>found by</th><th></th></tr></thead>
        <tbody>
          {rows.map((f, i) => (
            <tr key={i}>
              <td><span className={`badge ${f.severity === "high" ? "bad" : f.severity === "review" ? "hold" : "ok"}`}>{f.severity}</span></td>
              <td className="mono">{f.class}</td>
              <td>{f.title}</td>
              <td className="mono">{f.method}</td>
              <td><button className="btn ghost" onClick={() => setOpen(open === i ? null : i)}>
                {open === i ? "hide" : "evidence"}</button></td>
            </tr>
          ))}
        </tbody>
      </table></div>
      {open !== null && rows[open] && (
        <>
          <h3>Evidence</h3>
          <pre>{JSON.stringify(rows[open].evidence, null, 1)}</pre>
        </>
      )}
    </>
  );
}
