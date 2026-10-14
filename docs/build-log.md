# Build log: what the evals caught

These are real bugs I hit while building OntoForge. Each one was caught by an eval or a test, not
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

## Smaller ones

- A `month` column broke DuckDB parsing (reserved word). Renamed to `ym`.
- The ontology build took 7 seconds with row inserts. Arrow bulk inserts brought it to about 2.
- The audit hash check failed after a type round trip. Hashing a canonical JSON of the values fixed it.
- A LangGraph interrupt was logged as an error span. It's now an `interrupt` event, because pausing for a human is not a failure.
