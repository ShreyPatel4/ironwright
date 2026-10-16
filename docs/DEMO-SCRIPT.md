# The 90-second demo

This script is a gate. Any phase of work must change this script in the same
PR, or it is not a phase. CI runs `make demo-check` so the path below cannot
silently break.

Setup, once: `uv sync --all-extras && uv run ironwright run --pack all`
(about 50 s). Then `make demo` and open http://localhost:8001.

- 0:00  "Four fictional companies, each with a messy legacy estate the
        generator sabotaged on purpose. Every defect is written to ground
        truth and checked by an eval, so everything you are about to see is
        provable." Point at the four tabs, all `evals pass`.
- 0:15  Brightline Retail. The counter strip: 10 legacy feeds, 117,953 raw
        rows, 60,413 gold rows, 110 ontology objects, 27 agent decisions,
        evals 5/5. "That is the whole migration, landed to a lakehouse,
        reconciled, and lifted into an ontology."
- 0:35  Scroll to planted problems. "Duplicate vendors, a planogram version
        flip, late files. The platform caught each one; the eval checks it
        caught each one."
- 0:55  The agent trace. "The agent answers why is this item underperforming
        using only governed ontology calls. The model never picks an action;
        anything irreversible stops for a human."
- 1:15  The audit log. "Every action lands in a hash-chained audit log. On
        the public site your browser recomputes all 59 digests; rewrite one
        row and the chain catches it one link later."
- 1:30  The console: `uv run ironwright platform`, open
        http://localhost:8000. Four workspaces with derived verification
        badges; pick one and show the pipeline run with measured timings,
        the read-only SQL explorer on the lakehouse, the ontology browser
        with each identity's crosswalk, the Loads view showing every
        write's audit branch and checks (publish to main is the checks'
        decision, not a button), the Agents view investigating a triage
        decision down to its trace waterfall and the delist attempt the
        policy denied, the Ask surface answering through governed calls
        and refusing outside the ontology, Governance running a policy
        dry-run, and the audit chain verifying server-side.
- 1:40  The door: invite-only sign-in (local salted hashes, seeded by
        ironwright auth-set; the page says the API stays locally open
        until hosted auth is live). Wrong password bounces; the account
        decides the identity on every audit row.
- 1:42  Any object opens to its governed page: props with per-field
        provenance, the three-source identity, the audit trail that
        touched it, and action chips that render permit or deny from a
        live policy dry-run for whoever is signed in. Boards: tiles over
        the lakehouse, SELECT-only at save, last-run time always shown.
- 1:43  Discovery, the thesis in one screen: "Harbor Pet Foods LLC
        exists 3 times, twice in one system" found blind from the merge
        log, a failing coverage check, a reconciled difference, and the
        routine work summarized in one line. Empty classes stay empty.
- 1:45  First run opens the welcome funnel: three facts (real runs, the
        badge never lies, rules decide) and a template chooser that only
        picks where you land. The Platform map shows every capability,
        LIVE solid and clickable, PLANNED dashed with its dependency; the
        concept never wears a working button.
- 1:50  Create an organization: name, archetype, size, seed. The scenario
        engine generates the whole org (hierarchy, data owners, approval
        chains) in under a second, byte-identical per spec, badged L2
        because generated is not proven and the badge says which is which.
- 2:10  Done at the long cut; the 90-second cut stops at 1:30 before the
        console and says "and there is an operator console on top". If
        asked for depth: `uv run pytest -q` (69 tests).
