import { useEffect, useState } from "react";
import { api } from "../../api.js";
import { Err } from "../../components.jsx";
import Members from "./Members.jsx";
import Usage from "./Usage.jsx";
import ModelKey from "./ModelKey.jsx";
import OrgAudit from "./OrgAudit.jsx";
import OrgData from "./OrgData.jsx";
import Security from "./Security.jsx";

/* Your org's settings. The tabs follow your role as the server has it now, not as
   it was at sign in. Every route checks again, so hiding a tab is only manners. */
const TABS = [
  ["members", "Members", "admin", Members],
  ["usage", "Usage", "viewer", Usage],
  ["security", "Security", "viewer", Security],
  ["key", "Model key", "admin", ModelKey],
  ["audit", "Audit", "admin", OrgAudit],
  ["data", "Your data", "owner", OrgData],
];
const RANK = ["viewer", "member", "admin", "owner"];

export default function Settings({ onGone, onRotated, onSignedOut }) {
  const [me, setMe] = useState(null);
  const [active, setActive] = useState(null);
  const [gone, setGone] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => { api.me().then(setMe).catch(setErr); }, []);

  if (gone) {
    return (
      <>
        <h2>Settings</h2>
        <h3>This org is being deleted</h3>
        <p className="sub">Everyone in it is signed out. Its files, its lake and its records go next.
          One line stays in the audit log to say that it existed and who asked for this.
          It holds no names.</p>
        <button className="btn" onClick={onGone}>Back to sign in</button>
      </>
    );
  }
  if (!me) return <><h2>Settings</h2><Err e={err} /></>;

  const tabs = TABS.filter(([, , needed]) => RANK.indexOf(me.role) >= RANK.indexOf(needed));
  const key = active ?? tabs[0][0];
  const Active = tabs.find(([k]) => k === key)[3];
  return (
    <>
      <h2>Settings</h2>
      <p className="sub">The org <span className="mono">{me.org_slug}</span>, where your role is {me.role}.</p>
      <div className="row" role="tablist">
        {tabs.map(([k, label]) => (
          <button aria-selected={k === key} className={k === key ? "btn" : "btn ghost"} key={k}
                  onClick={() => setActive(k)} role="tab">{label}</button>
        ))}
      </div>
      <Active me={me} onDeleted={() => setGone(true)} onRotated={onRotated} onSignedOut={onSignedOut}
              slug={me.org_slug} />
    </>
  );
}
