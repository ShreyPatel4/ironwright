import { useEffect, useState } from "react";
import { api, getPrincipal, setPrincipal } from "../api.js";
import { Err } from "../components.jsx";

export default function Principals({ ws }) {
  const [data, setData] = useState(null);
  const [current, setCurrent] = useState(getPrincipal());
  const [err, setErr] = useState(null);

  useEffect(() => {
    setErr(null);
    api.principals(ws.name).then(setData).catch(setErr);
  }, [ws.name]);

  function actAs(p) {
    setPrincipal(p);
    setCurrent(p);
  }

  return (
    <>
      <h2>Principals</h2>
      <p className="sub">{data?.note ?? ""} Choosing one here changes who every console request
        acts as: masking, policy decisions, and the audit trail all follow it.</p>
      <Err e={err} />
      <div className="row">
        <span className="note">acting as</span>
        <span className="badge hold mono">{current}</span>
        <input onChange={(e) => setCurrent(e.target.value)} type="text" value={current} />
        <button className="btn" onClick={() => actAs(current)}>Act as this principal</button>
      </div>
      {data && (
        <>
          <h3>Principals named in policy</h3>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {data.policy_principals.map((p) => (
              <button className="btn ghost mono" key={p} onClick={() => actAs(p)}>{p}</button>
            ))}
          </div>
          <h3>Principals seen in the audit log</h3>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {data.seen_in_audit.map((p) => (
              <button className="btn ghost mono" key={p} onClick={() => actAs(p)}>{p}</button>
            ))}
          </div>
        </>
      )}
    </>
  );
}
