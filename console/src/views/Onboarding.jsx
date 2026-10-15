import { useState } from "react";

/* First-run funnel. Two screens, then the user's choice templates the boot
   sequence. The flag lives in localStorage because it is a per-viewer
   convenience; everything real stays server-derived. The third option is
   honest about being planned: it routes to the map, not to a fake wizard. */

const CHOICES = [
  {
    id: "demo",
    title: "Explore a proven demo",
    body: "Brightline Retail: a 16-store chain migrated end to end, with planted defects, "
          + "green evals and a hash-chained audit. The fastest way to see what the platform does.",
    cta: "Open Brightline Retail",
  },
  {
    id: "org",
    title: "Create my own organization",
    body: "The scenario engine generates a whole org from a name, archetype, size and seed: "
          + "hierarchy, data owners, approval chains. Byte-identical per spec, badged L2 because "
          + "generated is not proven.",
    cta: "Create an organization",
  },
  {
    id: "byod",
    title: "Bring my own data",
    body: "Planned, and this card will not pretend otherwise: the migration assistant that "
          + "profiles your files and helps you author the mapping is on the map with its "
          + "dependencies. Your bytes will never leave your machine.",
    cta: "See it on the platform map",
  },
  {
    id: "map",
    title: "Study the whole platform",
    body: "Every capability on one canvas: what is live on this machine, what is planned, and "
          + "what each planned piece depends on. The concept never wears a working button.",
    cta: "Open the map",
  },
];

export default function Onboarding({ onChoose, onSkip }) {
  const [step, setStep] = useState(0);

  return (
    <div style={{ margin: "6vh auto 0", maxWidth: 640 }}>
      <p className="note mono" style={{ letterSpacing: ".08em", textTransform: "uppercase" }}>
        ironwright · first run
      </p>
      {step === 0 && (
        <>
          <h2 style={{ fontSize: 24, margin: "10px 0 12px" }}>
            Legacy systems to an ontology, with agents that are safe to let act.
          </h2>
          <p style={{ fontSize: 14.5, lineHeight: 1.7, maxWidth: "62ch" }}>
            Ironwright takes a company that runs on old systems and walks it, stage by gated
            stage, onto a lakehouse, an ontology, and governed agents. Three things to know
            before anything else:
          </p>
          <div className="cards" style={{ margin: "14px 0 18px" }}>
            <div className="card" style={{ cursor: "default", gridTemplateColumns: "1fr" }}>
              <b>Every number is a real run.</b>
              <p>Pipelines execute on this machine; nothing on screen is a mockup.</p>
            </div>
            <div className="card" style={{ cursor: "default", gridTemplateColumns: "1fr" }}>
              <b>The badge never lies.</b>
              <p>L3 means planted ground truth and green evals here. L2 means generated.
                 L1 means your files, structural checks only. Derived from disk, never declared.</p>
            </div>
            <div className="card" style={{ cursor: "default", gridTemplateColumns: "1fr" }}>
              <b>Rules decide, models narrate.</b>
              <p>Agents act only through governed functions; anything irreversible stops for a
                 human, and every step lands in a hash-chained audit log.</p>
            </div>
          </div>
          <div className="row">
            <button className="btn" onClick={() => setStep(1)}>Choose a starting point</button>
            <button className="btn ghost" onClick={onSkip}>Skip the tour</button>
          </div>
        </>
      )}
      {step === 1 && (
        <>
          <h2 style={{ fontSize: 22, margin: "10px 0 12px" }}>What do you want to do first?</h2>
          <p className="note" style={{ marginBottom: 14 }}>
            This only picks where you land; every surface stays one click away.
          </p>
          <div className="cards">
            {CHOICES.map((c) => (
              <button className="card" key={c.id} onClick={() => onChoose(c.id)}
                      style={{ gridTemplateColumns: "minmax(0,14rem) 1fr" }}>
                <b>{c.title}</b>
                <p>{c.body}<br />
                  <span style={{ color: "var(--accent)", fontWeight: 500 }}>{c.cta} →</span>
                </p>
              </button>
            ))}
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <button className="btn ghost" onClick={() => setStep(0)}>Back</button>
            <button className="btn ghost" onClick={onSkip}>Skip</button>
          </div>
        </>
      )}
    </div>
  );
}
