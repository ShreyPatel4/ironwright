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
- 1:30  Done. If asked for depth: `uv run pytest -q` (62 tests), the
        pack API at :8000/docs, or the platform spine:
        `uv run ironwright platform` then `curl :8000/workspaces` shows
        every workspace with its derived verification level, and the
        approvals inbox and audit verify live at
        /workspaces/retail/approvals and /workspaces/retail/audit/verify.
