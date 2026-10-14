# Ironwright: notes for Claude Code

Ironwright is a Coconut Labs demo platform. It takes mock businesses with messy legacy systems and runs
them end to end: legacy files, an Iceberg lakehouse with quality gates, reconciliation, an ontology
with identity resolution, governed actions and LangGraph agents, with traces and evals at every layer.
Read `README.md` and `docs/architecture.md` first, then `docs/ROADMAP.md` for what to build next.

## Commands

```bash
pip install -e ".[dev]"
ironwright run --pack all        # full pipeline for every pack (about 50s)
pytest -q                       # must stay green: every pack passes every eval layer
make bundle                     # export snapshots + build dist/ironwright-showcase.html
make site                       # assemble site/ (landing, demo/, film/, films/, media/)
make preview                    # serve site/ on :8002
make media                      # re-render intro + six use-case films at 60fps into media/ (slow)
make deploy                     # wrangler pages deploy site (needs Cloudflare login)
node deploy/route-worker/test.mjs   # checks the coconutlabs.org/ironwright path route
```

## Site and deploys

- The public home is `coconutlabs.org/projects/ironwright` (house policy: projects live as
  routes, never subdomains). The standalone showcase build in `web/` still works locally and the
  route Worker in `deploy/route-worker` remains an option for serving it under a path. Keep every
  link relative and every directory URL ending in a slash, or path serving breaks.
- `main` deploys production, `staging` deploys a preview behind Cloudflare Access, PRs get previews.
- Rendered MP4s are committed in `media/`. Films are generated from `web/films/specs/*.json`, which
  `scripts/film_specs.py` builds from each pack's real export. Never hand-edit numbers for a real pack.
- Visual language everywhere: Geist and Geist Mono, flat panels, 1px borders, 6px corners, one blue
  accent, semantic colors only for state. Slow, calm motion: one easing curve, content holds for a
  few seconds before it moves on.

Set `IRONWRIGHT_DATA` to keep generated data out of the repo (the Makefile does this).

## Rules that keep the project honest

- **Evals gate everything.** A change is done only when `pytest -q` passes. If an eval fails, check the
  ground truth before changing engine code (see `docs/build-log.md` #4).
- **Ground truth comes from generators.** Every planted problem must be written to `_ground_truth.json`
  and checked by an eval. No planted problem without an eval, no eval without a planted problem.
  This contract applies to GENERATED workspaces (verification level L3, and L2 once scenario packs
  run). A local workspace built from the user's own files (L1) has no planted problems, so it
  carries structural and reconciliation checks only; the ground-truth contract is not vacuous
  there, it is explicitly out of scope, and the UI badge says so.
- **Order inside `act()` is fixed:** idempotency, policy, params, preconditions, approval, edits,
  outbox, audit. Policy always runs before validation.
- **Rules decide, the LLM narrates.** Never let model output pick an action. Narration must cite
  `[ev:<id>]` ids the agent actually gathered; the guardrail rewrites anything else.
- **Nothing irreversible before `interrupt()`.**
- **Default deny.** New actions need an explicit permit in the pack's `policies.yaml`.
- **Packs are data, the engine is generic.** Industry logic goes in `ironwright/packs/<pack>/`, never
  in engine modules.
- **Synthetic demos; local-first real data.** The DEMOS are synthetic and the platform never
  uploads anything anywhere. A user may point their LOCAL instance at their own files (an L1
  workspace); those bytes stay on their machine, never reach a hosted service of ours, and are
  treated as untrusted at every prompt boundary. Hosted multi-tenant ingestion of real data
  remains out of scope. See the security section of `docs/ROADMAP.md`.

## Writing style for docs and UI copy

First person, plain words, short sentences. Never use em dashes. Prefer a diagram (mermaid) when
explaining a flow.

## Layout

- `ironwright/` engine (legacy, lake, quality, reconcile, ontology/, policy, agents/, tracing, evals, api, mcp_server, export, cli)
- `ironwright/packs/<pack>/` one folder per mock business
- `web/` static demo (vanilla JS), `web/intro/` the film, `web/films/` use-case films, `web/site/` landing page
- `scripts/` bundle, build_intro, film_specs, build_films, build_site
- `console/` the operator console (Vite + React source; built dist/ committed)
- `media/` rendered MP4s and posters (committed)
- `deploy/` Cloudflare Pages, route Worker, narration Worker, free tier and AWS notes
- `docs/` architecture, roadmap, kickoff prompts, interview walkthrough, build log
