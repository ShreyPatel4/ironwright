# The 90-second demo

This script is a gate. Any phase of work must change this script in the same
PR, or it is not a phase. CI runs `make demo-check` so the path below cannot
silently break.

Setup, once: `uv sync --all-extras && uv run ironwright run --pack all`
(about 50 s). Then `make demo` and open http://localhost:8001.

- 0:00  "Five fictional companies, each with a messy legacy estate the
        generator sabotaged on purpose: Brightline Retail, Corvane
        Distribution, Harborview Bank, Granite Mutual and Quillback Casualty.
        Every defect is written to ground truth and checked by an eval, so
        everything you are about to see is provable." Point at the tabs, all
        `evals pass`.
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
- 1:30  The console, dev open: `IRONWRIGHT_DEV_OPEN=1 uv run ironwright
        platform`, open http://localhost:8000. Say that this beat runs with
        the door open, and point at the red badge. The demo workspaces have
        derived verification badges, and Quillback's is the insurance estate
        with bitemporal coverage; pick one and show the pipeline run with measured timings,
        the read-only SQL explorer on the lakehouse, the ontology browser
        with each identity's crosswalk, the Loads view showing every
        write's audit branch and checks (publish to main is the checks'
        decision, not a button), the Agents view investigating a triage
        decision down to its trace waterfall and the delist attempt the
        policy denied, the Ask surface answering through governed calls
        and refusing outside the ontology, Governance running a policy
        dry-run, and the audit chain verifying server-side.
- 1:40  The door, closed: restart without IRONWRIGHT_DEV_OPEN and press
        Sign up. Anyone can, and each sign-up gets its own org. Every API
        route needs a session. No token is a 401, a spoofed X-Principal is
        ignored, and an agent principal can never sign in. Sessions die after
        8 idle hours and rotate on login. Wrong password bounces; the account
        decides the identity on every audit row. Then sign up a second org.
        Org B asks for org A's job id and gets a 404 with the same body as
        an id nobody has: `{"detail": "no such resource"}`.
        IRONWRIGHT_DEV_OPEN=1 stays for tests and demo-check, and staging and
        prod refuse to start with it.
- 1:41  The connector kinds: object-store drops, ADBC warehouses (the
        postgres driver proven against a real server in CI), SAP IDoc
        flat files, stream windows from recorded segments, and CDC from
        postgres logical decoding with consume-once slots, each
        fixture-proven, with what is NOT claimed written on the Sources
        page itself.
- 1:41b A created org generates its own governance: the diff is the
        review, applying is logged, and a conflict with a hand-written
        rule drops the generated rule and says so. A denied action chip
        on any object page escalates to the owning role in one click;
        the grant is a narrow rule that every serving decision consults
        (act, the dashboard chips, the governance tester, Ask) until it
        dies at the start of its expiry date, and hand-written forbids
        still win. Objects read as-of any moment; traces export as OTLP.
- 1:41c Training mode replays approvals
        and denials from the audit log into labels and evaluates a
        proposed rule change against the human verdicts before anyone
        adopts it (nothing retrains silently); the engine swap runs
        every retail model unchanged on postgres and compares value for
        value from the Data explorer; silver and gold mirror into
        postgres with a verified round trip and your BI tool renders in
        a sandboxed frame.
- 1:41d The serving posture: locally, both servers bind 127.0.0.1, and
        anything else refuses to start without --host and
        --i-know-this-is-exposed. The hosted stack binds inside its container
        and publishes on loopback. IRONWRIGHT_KILL_SWITCH=1 and every
        write answers 503 while reads stay up. docs/threat-model.md maps
        each control to the test that proves it, and a test fails if a
        link goes stale. demo-check does not walk this beat, because it
        runs the app without harden(); tests/test_posture.py proves it.
- 1:41e Real bytes meet three more walls. SQL: read_text('/etc/passwd'),
        COPY and ATTACH fail closed in the explorer and in board tiles, a
        cartesian bomb hits the timeout, and an error is a short message
        with an id. Paths: /workspaces/%2e%2e stays a refusal, and an L1
        folder is read only, refusing symlinks that escape, FIFOs and
        oversize files. Prompts: a cell that says "ignore previous
        instructions" reaches the model inside an untrusted fence, the
        guardrail drops what it did not earn, and rules still pick every
        action.
- 1:42  Any object opens to its governed page: props with per-field
        provenance, the three-source identity, the audit trail that
        touched it, and action chips that render permit or deny from a
        live policy dry-run for whoever is signed in. Boards: tiles over
        the lakehouse, SELECT-only at save, last-run time always shown.
- 1:43  Discovery, the thesis in one screen: "Harbor Pet Foods LLC
        exists 3 times, twice in one system" found blind from the merge
        log, a failing coverage check, a reconciled difference, and the
        routine work summarized in one line. Empty classes stay empty.
- 1:45  The Platform map shows every capability, LIVE solid and clickable.
        Anything not built yet would be dashed with its dependency, and the
        concept never wears a working button. Today nothing on it is dashed.
- 1:50  Create an organization: name, archetype, size, seed. The scenario
        engine generates the whole org (hierarchy, data owners, approval
        chains) in under a second, byte-identical per spec, badged L2
        because generated is not proven and the badge says which is which.
- 1:55  The beat that sells it: your own files. Signed in as org A, walk the
        wizard over your export: upload, profile, map, run, ontology, then
        a grounded answer in Ask that cites the evidence it gathered. Org B
        on the same server cannot see any of it. Next month's export arrives:
        Sources, Upload new version. A new column is `info` and the run goes
        on. A missing key is `block`, the run stops, and Ask keeps serving
        the last good data until I upload a corrected file and run again. Add
        another dataset from the same screen. Settings, delete the org, and nothing
        is left but the promised audit line.
- 2:10  Done at the long cut; the 90-second cut stops at 1:30 before the
        console and says "and there is an operator console on top". If
        asked for depth: `uv run pytest -q`. The Postgres tests need
        POSTGRES_URI on a server with wal_level=logical, and the object store
        tests need IRONWRIGHT_TEST_S3_ENDPOINT. Without them they skip and say
        so (the review gates at the end of test_platform_api.py each pin a
        fixed defect). CI's `make demo-check` walks the pipeline half AND every console
        phase above headlessly, bounded at 300 s.
