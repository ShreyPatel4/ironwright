import { useEffect, useState } from "react";
import { api } from "../api.js";

/* The entry gate. This local build authenticates nobody, and lying about
   that would be worse than the missing feature: so the door is an
   IDENTITY CHOICE, labeled as exactly that. Who you choose rides every
   request, decides what policy permits and what masking hides, and is
   written on every audit row you cause. Real authentication arrives with
   the hosted build, not before. */

const SUGGESTED = [
  ["user:console", "Operator: the console default, broad read access"],
  ["user:category.manager.1", "Category manager: may act, approvals apply"],
  ["vendor:V-00001", "A vendor: sees only their own slice, most things masked"],
];

export default function Login({ onEnter }) {
  const [principal, setPrincipal] = useState("user:console");
  const [known, setKnown] = useState([]);

  useEffect(() => {
    api.principals("retail")
      .then((p) => setKnown([...new Set([...p.policy_principals, ...p.seen_in_audit])]))
      .catch(() => {});
  }, []);

  function enter(p) {
    const v = (p ?? principal).trim();
    if (v) onEnter(v);
  }

  return (
    <div style={{ margin: "10vh auto 0", maxWidth: 520, padding: "0 16px" }}>
      <p className="note mono" style={{ letterSpacing: ".08em", textTransform: "uppercase" }}>
        ironwright console
      </p>
      <h2 style={{ fontSize: 24, margin: "8px 0 6px" }}>Choose who you are operating as.</h2>
      <p style={{ color: "var(--ink-1)", fontSize: 14, lineHeight: 1.65, margin: "0 0 18px" }}>
        Everything here is governed by principal: what policy permits, what masking hides,
        and what the audit log records all follow this choice. This local build does not
        authenticate; your choice is an identity claim, recorded on every action you take.
      </p>
      <div className="cards" style={{ marginBottom: 16 }}>
        {SUGGESTED.map(([p, label]) => (
          <button className="card" key={p} onClick={() => enter(p)}
                  style={{ gridTemplateColumns: "minmax(0,14rem) 1fr" }}>
            <b className="mono" style={{ fontSize: 13 }}>{p}</b>
            <p>{label}</p>
          </button>
        ))}
      </div>
      <div className="row">
        <input list="iw-principals" onChange={(e) => setPrincipal(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && enter()}
               placeholder="or type any principal, e.g. role:controller"
               style={{ flex: 1 }} type="text" value={principal} />
        <datalist id="iw-principals">
          {known.map((p) => <option key={p} value={p} />)}
        </datalist>
        <button className="btn" onClick={() => enter()}>Enter the console</button>
      </div>
      <p className="note" style={{ marginTop: 14 }}>
        You can change identity any time under GOVERN, Principals. Hosted authentication is a
        planned node on the Platform map, and until it is live this page will keep saying so.
      </p>
    </div>
  );
}
