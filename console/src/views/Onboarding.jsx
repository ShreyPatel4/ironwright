import { useCallback, useEffect, useState } from "react";
import { Err } from "../components.jsx";
import AskStep from "./onboarding/AskStep.jsx";
import MapStep from "./onboarding/MapStep.jsx";
import OntologyStep from "./onboarding/OntologyStep.jsx";
import ProfileStep from "./onboarding/ProfileStep.jsx";
import RunStep from "./onboarding/RunStep.jsx";
import { canWrite, furthestStep, Loading, loadJourney, Panel, STEPS } from "./onboarding/shared.jsx";
import UploadStep from "./onboarding/UploadStep.jsx";

/* The way in: from an empty org to a grounded answer over its own files. Which
   step you land on is worked out from the server's uploads, jobs and workspaces.
   Nothing about the journey is kept in this browser. */

function Account({ me }) {
  return (
    <>
      <h2>Account</h2>
      <Panel>
        <p style={{ margin: 0 }}>You are <b>{me.email}</b>, with the role <b>{me.role}</b>.</p>
        <p className="note" style={{ margin: "6px 0 0" }}>
          Your org's data is visible to your org and nobody else.
        </p>
      </Panel>
    </>
  );
}

const VIEWS = { upload: UploadStep, profile: ProfileStep, map: MapStep, run: RunStep,
                ontology: OntologyStep, ask: AskStep };

export default function Onboarding({ onChoose, onSkip, onSignOut }) {
  const [journey, setJourney] = useState(null);
  const [viewing, setViewing] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    setErr(null);
    try { setJourney(await loadJourney()); } catch (e) { setErr(e); }
  }, []);
  useEffect(() => { load(); }, [load]);

  // A finished step asks for the next one, and the server decides which. Upload names
  // Profile, because a quick worker can finish before you have read the result.
  const advance = useCallback(async (to = null) => {
    await load();
    setViewing(to);
  }, [load]);

  const furthest = journey ? furthestStep(journey) : null;
  const reached = STEPS.findIndex(([k]) => k === furthest);
  const current = viewing ?? furthest;
  const View = VIEWS[current];

  return (
    <div style={{ "--rad": "6px", margin: "4vh auto 40px", maxWidth: 920, padding: "0 16px" }}>
      <p className="note mono" style={{ letterSpacing: ".08em", margin: 0, textTransform: "uppercase" }}>
        ironwright · bring your own data
      </p>
      {journey && (
        <nav aria-label="steps" className="row" style={{ margin: "12px 0 20px" }}>
          {STEPS.map(([key, label], i) => (
            <button aria-current={current === key ? "step" : undefined}
                    className="btn ghost" disabled={i > reached} key={key}
                    onClick={() => setViewing(key)}
                    style={{ borderColor: current === key ? "var(--accent)" : undefined,
                             color: current === key ? "var(--accent)" : undefined,
                             fontSize: 12.5, padding: "4px 10px" }}
                    type="button">
              {i + 1} {label}
            </button>
          ))}
        </nav>
      )}

      {!journey && !err && <Loading what="your org's progress" />}
      {err && (
        <div>
          <Err e={err} />
          <button className="btn" onClick={load} type="button">Try again</button>
        </div>
      )}

      {journey && !canWrite(journey.me.role) && (
        <p className="note">You have read access here. You can see every step and change nothing.</p>
      )}
      {journey && current === "account" && <Account me={journey.me} />}
      {journey && View && <View advance={advance} journey={journey} key={current} />}

      <div className="row" style={{ marginTop: 22 }}>
        <button className="btn ghost" onClick={() => onChoose("demo")} type="button">
          Explore a demo instead
        </button>
        <button className="btn ghost" onClick={onSkip} type="button">Go to the console</button>
        <button className="btn ghost" onClick={onSignOut} type="button">Sign out</button>
      </div>
    </div>
  );
}
