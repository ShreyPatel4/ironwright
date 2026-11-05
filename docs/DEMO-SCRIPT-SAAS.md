# The hosted product demo, v2

This is the story of Ironwright as a SaaS product: one insurer's messy files, from sign-up to a
grounded answer and a governed action, with a second org on the same stack refused on screen.
`docs/DEMO-SCRIPT.md` is still the script for the synthetic packs and the `make demo-check` gate.

The film is a recording of the real console on the compose stack. Nothing is stubbed. Each beat
checks what the server answered, and a beat that cannot happen stops the take.

```mermaid
flowchart LR
  S[Sign up] --> U[Upload 3 messy files]
  U --> P[Profile: reasons and joins]
  P --> D[Silver draft: a person fixes a key]
  D --> R[Run: bronze, silver, gold]
  R --> O[Ontology: a person accepts]
  O --> A[Ask: 2 cited, 1 refused]
  A --> G[Gated action: a person approves]
  G --> C[Audit chain verifies]
  C --> I[Rival Co gets a 404]
  I --> Q[Usage and quotas]
```

## The tenants

`scripts/seed_demo.py` makes these and changes nothing the second time. It refuses prod, and the
password comes from `IRONWRIGHT_SEED_PASSWORD`, never from the repo.

| Who | Org | Role | What it is for |
|---|---|---|---|
| `owner@northwind-mutual.example` | Northwind Mutual | owner | A prospect logs in here and walks the wizard from Profile on |
| `viewer@northwind-mutual.example` | Northwind Mutual | viewer | The read only view of the same work |
| `owner@rival-co.example` | Rival Co | owner | The isolation beat. It holds nothing |

Northwind's files are the `insurance_policy` pack's legacy exports, taken as uploads, with no pack
behind them and no answer key:

| File | The mess |
|---|---|
| `POLADMIN.dat` | Fixed width with no header, so its columns arrive as `field_1` onward |
| `BILLING.csv` | The only place a lapse is recorded. Points at policies by number |
| `AGENCY_BOOK.csv` | An agency's own sheet: `Vale, Jordan`, dates like `3/13/1965`, masked ids |

The film signs up its own new tenant, also named Northwind Mutual, with the same bytes. A sign-up
has to be on camera, and the seeded org already exists. Slugs stay unique, so the seed and the film
can run on one stack in either order, and a take can be run twice. The film needs only Rival Co
from the seed.

## The film, beat by beat

Target 2:30. Job waits are real and get cut down to a short lead and tail. Captions come from
`scripts/demo_film.json`, and a test holds this table to that file.

| Time | Beat | What is on screen | Caption |
|---|---|---|---|
| 0:00 | signup | The door, then a new org | Northwind Mutual is an insurer with old systems. It signs up here, on the hosted stack. |
| 0:10 | upload | Three files chosen | Three real shapes of mess: a fixed width policy file, a billing export, an agency's spreadsheet. |
| 0:18 | profile | Types with reasons, then the join on `policy_nbr` | I read each file by rules, not by model. Every guess comes with its reason. Nobody told me where the joins are. I found them from the values. |
| 0:34 | silver | Decisions with row counts. The person swaps the key on `agency_book` from `client_key` to `policy_nbr` | No SQL to write. I draft the cleaning and count the rows each decision touches. I guessed client_key. A person knows policy_nbr is the real key, so they change it. |
| 0:52 | run | The job and its layers, with the L1 badge | Bronze, silver and gold run as a background job, with quality gates on the way. The badge says L1: real files, so no planted answers to check against. I say so. |
| 1:05 | ontology | Objects, then the Billing to AgencyBook link | I propose objects and links, each with its reason. Nothing goes live until a person accepts. |
| 1:18 | ask | Two cited answers, one refusal | Ask a real question. Rules gather the facts, and every claim cites what it stands on. The evidence ids are what the agent really gathered. Anything else gets rewritten. This one is out of reach of the ontology, so I refuse and say why. |
| 1:45 | action | A parked action in Approvals, then the decision | Now an action. The policy parks it, and nothing changes until a person decides. Approved, with that person's identity on the record. |
| 2:00 | audit | Verify the chain | Every step lands in a hash chain. The server recomputes all of it on demand. |
| 2:08 | isolation | Rival Co's empty list, then the raw 404 on Northwind's upload id | Rival Co shares this stack. Signed in there, none of Northwind's work is listed. Rival asks for Northwind's real upload id. It gets the same 404 as an id that never existed. |
| 2:22 | usage | The plan and its limits | Back at Northwind: what it used today, against its plan's limits. That is the whole journey, on the same stack a customer gets. |

A caption that claims something has a check behind it. The profile beat fails if no join is listed,
the ontology beat fails if the proposal has no link, and the ask beat fails if a citation is not in
what the agent gathered.

## Record it

```bash
docker compose -f deploy/compose.yaml up --build -d
docker compose -f deploy/compose.yaml exec -T -e IRONWRIGHT_SEED_PASSWORD=... api \
    python - < scripts/seed_demo.py
IRONWRIGHT_SEED_PASSWORD=... python scripts/record_demo.py --base http://127.0.0.1:8000
```

That writes `media/demo/ironwright-saas-demo.mp4` and its poster. It stops at the first gap, and it
refuses a film that is more than 25 seconds off the target. Then take the strict xfail off
`test_the_film_and_the_poster_are_committed`.

To try it out, add `--rehearse <folder>`. A rehearsal carries on past gaps, lists them, and can never
write into `media/`.

## What stands between this and the final take

| Beat | What it needs | Who |
|---|---|---|
| silver | F2's silver draft (#39) | F2 |
| action | Tenant routes for approvals and actions (#24), plus a tenant action with `requires_approval` and a policy that permits it. Workspaces hold no actions today | Nobody yet |
| audit | Tenant routes for the audit log (#24) | Nobody yet |
| every beat | The compose stack, for the take itself | Me |
