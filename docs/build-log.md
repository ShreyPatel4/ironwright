# Build log: what the evals caught

These are real bugs I hit while building Ironwright. Each one was caught by an eval or a test, not
by reading code. They're the best part of the project to talk about, because they're the kind of
thing that goes wrong in real migrations.

## 1. Validation before authorization leaked information

**Symptom.** The safety test "agent cannot cancel a shipment" failed. The action came back
`rejected: missing param reason` instead of `denied`.

**Cause.** `act()` checked parameters and preconditions before policy. A forbidden caller got
told how to make a well-formed request.

**Fix.** Policy runs first, always. The test now asserts the stronger property: without
permission, the action never applies, whatever else is wrong with the request.

## 2. A policy limit with no escalation path

**Symptom.** Supply chain agent accuracy dropped on expensive shipments. Expedites over $500 were
denied, and the agent had nowhere to go.

**Cause.** The limit was right, but the design stopped there. Real teams have an approval route.

**Fix.** Added `propose_expedite`, an approval-gated action, plus a fallback in `usecases.yaml`.
Now the trace reads: expedite denied by policy, propose_expedite needs approval, human approved,
applied.

## 3. Block vs quarantine

**Symptom.** In insurance, one semantic check (loss reported before it happened) blocked the
whole claims table, so every downstream layer was empty.

**Cause.** Two FNOL rows from an app with a bad clock. Blocking the table punished thousands of
good rows for two bad ones.

**Fix.** A third severity, `quarantine`: bad rows go to `claims_fnol__quarantine` with the check
name, good rows publish. The evals check that exactly the planted rows were quarantined.

## 4. The eval was right and the ground truth was wrong

**Symptom.** Insurance agent accuracy was 70%. The confusion matrix showed the agent sending
"fast track" claims to coverage review.

**Cause.** The agent was correct. The generator was creating claims on policies that had already
expired, and labeling them fast track. The eval disagreeing with the agent exposed a bug in the
test data.

**Fix.** Claims now land within the policy's effective window. Lesson: when an eval fails, check
the ground truth before you "fix" the model.

## 5. The legacy report was wrong too

**Symptom.** Reconciliation failed for banking: the new CTR counts were higher than legacy.

**Cause.** The legacy job only flagged single cash transactions over $10,000. It never
aggregated same-day deposits, which the rule requires.

**Fix.** Not a code fix. A `known_differences` rule documents it (new > legacy is expected,
here's why), so cutover can proceed with the difference explained instead of hidden.

## 6. The route sweep could not see the routers

**Symptom.** The test that sends no token to every route crashed on an entry with no `path`. My
structural check for the `X-Principal` header then found nothing at all, not even the one route I
knew still declared it.

**Cause.** Newer FastAPI keeps an included router as one lazy entry in `app.routes` instead of
flattening it. A flat loop either crashes or skips every included route, and `pyproject.toml` lets
the version float, so which one you get depends on the install.

**Fix.** The walker recurses into includes and fails on anything it cannot classify. The sweep also
asserts it reached one route from each included router. It passes on FastAPI 0.143 and 0.162.

The same sweep caught `/docs`, `/redoc` and `/openapi.json` answering with no token. They sit outside
route dependencies, so I turned them off. And a test with a real token caught `POST /actions/{action}`
taking its actor from the request body, which let any session act as an agent. The actor now comes
from the session.

A real parked approval caught one more. `approved_by: "agent:spoofed"` on `/decide` applied the action.
It now gets a 422. A role can still sign, because tying a person to a role needs a model I don't have yet.

## 7. A delete that could leave the data root

**Symptom.** A new test sent `DELETE /workspaces/%2e%2e` and the folder above the data root was gone.
The same spelling on `GET /workspaces/%2e%2e/organization` returned an `org.json` planted up there.

**Cause.** The server decodes `%2e%2e` to `..`, and that is one path segment, so it matches the route.
Only create checked the name. Read and delete joined it straight onto the data root. Delete trusted
`scenario.yaml` as proof that I made a folder, and any folder can hold one of those.

**Fix.** Every request value now goes through `safe_child`: a regex on the name, then the resolved path
must equal `root/name`. Delete needs a marker written at create that names the folder. Create refuses
any folder that already exists.

The same tests found two quieter ones. An organization named `auth` moved into the credentials folder,
so deleting it deleted the credentials. And the slug regexes used `$` with `.match`, which lets a trailing
newline through. Those are now a reserved name and `fullmatch`.

## 8. A write that starts like a read

**Symptom.** The new SQL attack table failed on its first run. `with x as (select 1) insert into ...`
got past the guard, at the Explorer and at board save. So did update, delete, unicode semicolons
and a NUL byte. Every engine error also sent DuckDB's raw text back to the client, paths included.

**Cause.** The guard was a regex on the first word, copied into two files. `WITH` looks like a read,
but DuckDB's parser calls that statement an INSERT. The parser also stops at a NUL, so it can see
less of the string than I do.

**Fix.** One guard in `sqlguard.py`. It keeps the text checks and adds the real parser. I need both,
because `pragma version` parses as a SELECT. Clients now get a short category and an error id, and
the full text goes to the server log. The engine lockdown held throughout: no attack in the table
read or wrote a file, before or after the fix. The guard was the part that was wrong.

## 9. A cell could cite evidence with no model in the room

**Symptom.** I planted `[ev:fake#note]` and a markdown link in a cell for the prompt boundary test.
Both showed up in the Ask answer and in the agent narrative, with the LLM set to offline. The agent
still reported `grounded: True`.

**Cause.** I only ever guarded model output. The templates interpolate cell values too, and
`web/app.js` draws anything shaped like `[ev:...]` as a citation. On the synthetic packs no cell ever
said anything like that, so nothing caught it.

**Fix.** Templates go through the same guardrail as model text, and each cell is scrubbed on its own
so the authored sentence survives. Lesson: untrusted text has more than one way out. Guard the exit,
not the one path I was thinking about.

## Smaller ones

- A `month` column broke DuckDB parsing (reserved word). Renamed to `ym`.
- The ontology build took 7 seconds with row inserts. Arrow bulk inserts brought it to about 2.
- The audit hash check failed after a type round trip. Hashing a canonical JSON of the values fixed it.
- A LangGraph interrupt was logged as an error span. It's now an `interrupt` event, because pausing for a human is not a failure.
- The new link filter read customer emails as links. Scanning every string cell of every pack through the guardrail found it; no narration happened to carry one, so no eval would have. The bare domain pattern now skips anything after an `@`.
