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
## 10. The MCP server could not start, and nothing noticed

**Symptom.** The first test that built the MCP server died on import: `No module named
'mcp.server.fastmcp'`.

**Cause.** `pyproject.toml` asks for `mcp>=1.2` and the lockfile had moved to 2.3, which renamed
`FastMCP` to `MCPServer`. The README advertised `ironwright mcp`, but no test ever built the server
and the CLI imports it lazily, so the whole suite stayed green.

**Fix.** Moved to `MCPServer`, and `tests/test_posture.py` now builds the server and calls its tools.
Lesson: a surface with no test is a claim, not a feature.

## 11. A 404 proves nothing until the owner gets through

**Symptom.** The first isolation sweep passed for B on every route. Then I added the other half, where A walks the same routes and must get past the gate, and it failed on `DELETE /workspaces/{name}/boards/{slug}`.

**Cause.** The bug was in my test. It deleted the workspace partway through the sweep, so every later route was 404 for A too. But it showed me what the B half alone is worth: a gate that refuses everyone passes it, and so does a test aimed at a name that never existed.

**Fix.** The delete goes last. Each route now has to do three things: stop B, give B the same bytes it gets for a name nobody has, and let A through. I also broke the gate on purpose in seven ways and watched the file go red each time. Lesson: every "cannot see" test needs a "can see" twin, or it only proves the request failed.

## 12. Green on my branch, red on the merge

**Symptom.** Everything passed for me, then CI failed `test_rate_limit_window_expires` and the threat model link check. Neither test was in my tree.

**Cause.** S5 merged to main after I branched, and a pull request tests the merge. S5 brought a login limiter, and I had written a small one because S5 was not there yet. Stacked, each kept its own clock, so mine kept refusing after S5's let go. I had also deleted a test the new threat model links to.

**Fix.** I rebased, deleted my limiter, and pointed S5's at signup and accept-invite too, so there is one limiter on one clock. Lesson: fetch and rebase before the last test run, because the tree I test has to be the tree that merges.

## 13. The contract's own example did not parse

**Symptom.** The first run of the tenant fixture failed with `sources.claims.columns: unknown key 2)`.
The fixture used the column spelling from contract 3 word for word.

**Cause.** `{name: amount, type: decimal(12,2)}` is a YAML flow mapping, so the comma ends the value.
YAML reads `type: decimal(12` and a bare key `2)`. I wrote the example without ever loading it.

**Fix.** The loader puts the two halves back together, so the contract's spelling works, and a test
loads the example as written. Strict validation found this. A loose loader would have stored a broken
type and carried on. Lesson: load the examples in a contract before you build on them.

**Same run, second catch.** `private_dir` only closes folders it creates. A `tenants/` folder that
already existed with mode 0755 stayed open, so every org's folder name could be listed. `ensure_tenant`
now tightens the whole tree on every create.

## 14. A grant that outlived the run

**Symptom.** `make demo-check` was green on a clean data dir and red on the second run. The step
"a grant is policy the serving engine consults" reported BROKEN, because the first check, which should
deny, already allowed.

**Cause.** I moved the demo packs to `demo/<pack>/`, but `orgpolicy._target_dir` still built
`data_root()/<pack>` by hand. Generated policy landed outside the folder `generate` wipes, so grant
`esc-0001` from the last run stayed in `data/retail/policies.generated.yaml` and kept permitting. The
suite stayed green because every test starts from an empty root. Only a reused data dir shows it.

**Fix.** `_target_dir` asks `PackPaths`, and a test writes a grant, regenerates and expects no rules.
Lesson: when a path moves, grep for everyone who spelled it by hand, and run the gate twice on one dir.
## 15. A race test that a dead thread could pass

**Symptom.** I removed the claim lock on purpose to see the race tests fail. SQLite failed. The
Postgres half of the one-running-per-org test stayed green.

**Cause.** Without the lock, a worker hit the unique index and died with an `IntegrityError` on its
thread. The test only counted what the surviving threads held, and that count was still right. A
thread that dies leaves fewer claimers, which hides the very race the test exists to find.

**Fix.** The race helper now fails on any thread error, and the global cap has its own test with one
job per org, so only the cap can stop a claim. Lesson: a concurrency test has to prove every thread
finished, not just check the final tally. And delete the safeguard once to see the test go red.

## 16. The profiler looked right until I ran it on a real file

**Symptom.** My hand built fixtures passed. Then I pointed the profiler at what the retail generator
really writes. `STORMAST.dat` got a boundary in the middle of `Brightline #101`, and a join came back
between `field_3` and `field_1`. The join was real, by luck.

**Cause.** One blank column after text that never changes is not a field gutter, it is a word break
in a constant. And `difflib` scores `field_3` and `field_1` as similar, but I made those names up, so
they are no evidence of anything. My own review found a third one the same day: capping a column's
values at the first N seen meant two big files in different row orders compared different slices of
the ids and reported no overlap.

**Fix.** A lone gutter after constant text is not a boundary. Generated names and siblings like
`addr_1` and `addr_2` score zero. The value cap is now a fixed hash sample, so both sides keep the same
slice. The real generator files are in the test suite now. It pins the two boundaries alignment can
never see too (abutting unpadded digits, abutting single letters), so that limit is written down and
not a surprise. Lesson: a fixture I built only proves what I already thought of.

## 17. My proposal passed my copy of the schema and failed the real one

**Symptom.** T2 merged, so I ran a proposal through it end to end. `create_workspace` refused two of my three fixtures. One had `type: bool`, the other had `header` in a fixed width layout. A UTF-32 file would have failed too.

**Cause.** Until then the check was `validate_contract3`, my own strict copy of contract 3. I wrote it from the same reading of the contract as the code it checked, so it agreed with me. The contract never spells out a flag type, and T2 chose `boolean`.

**Fix.** The proposal now says `boolean`, leaves `header` off fixed width layouts, and leaves out a file the run cannot read, with a decision saying why. A fixed width header is on the record as a contract gap. The test now calls T2's validator, and each fixture goes from upload to a green `run_workspace`. Lesson: a stand-in for someone else's check only proves I agree with myself, so replace it the day the real one lands.
## 19. A typo that read as a full org

**Symptom.** With every member seat taken, inviting `not an email` answered 429 instead of 422.

**Cause.** The seat check runs before the invite does, and it compared the raw text to the seats. The
invite is what validates an email, so it never got to speak. The admin would be told to free a seat to
fix a typo.

**Fix.** The seat check cleans the email the same way the invite does, and steps aside when it cannot, so
the invite refuses it in its own words. While I was there I took out `pydantic` on `PUT /llm-key` and put
it back to see what it does: its 422 echoes the body, which here is a live key. That route reads its own
body, and the leak test sends the three shapes that echo.

## 18. A key edit could pick what the proposer never would

**Symptom.** Reading the first golden proposal, I saw `premium` marked unique. A new test confirmed that a `set_key` edit took it, and took `loss_date` too.

**Cause.** The proposer refuses measures and dates as keys, but the edit path only asked if the column was unique. Three policies with three premiums are unique today and collide on the next load. The same rule lived in one of the two places that needed it.

**Fix.** `set_key` applies the same type rule, with a test for each. Lesson: a human edit goes through the rules a proposal goes through. Approving something does not make it valid.

## Smaller ones

- A `month` column broke DuckDB parsing (reserved word). Renamed to `ym`.
- The ontology build took 7 seconds with row inserts. Arrow bulk inserts brought it to about 2.
- The audit hash check failed after a type round trip. Hashing a canonical JSON of the values fixed it.
- A LangGraph interrupt was logged as an error span. It's now an `interrupt` event, because pausing for a human is not a failure.
- The new link filter read customer emails as links. Scanning every string cell of every pack through the guardrail found it; no narration happened to carry one, so no eval would have. The bare domain pattern now skips anything after an `@`.
- My hosted entrypoint built the platform app straight, so it served without S5's hardening: no security headers, no origin guard, no login limit. S5 installs that in `cli._serve`, and a second road to uvicorn skipped it. `tests/test_hosted.py` now checks the headers and the origin guard on the hosted app.
- The first compose run could not pull MinIO. Docker Hub has dropped the repos and `dl.min.io` answers 410: the open-source project is archived with no security updates. The image pin looked fine in review and only a real pull found it. Compose now runs Versity's S3 gateway, and the bucket init uses our own image, so `mc` is gone too.
- The same CI job piped pytest into `tee` under `bash -e` with no pipefail, so failing tests would have read as green. The job now states `shell: bash`, which adds pipefail.
- Every real tenant run refused to start on the hosted lake. My guard read `org_id` off the pack, T2's `Pack` has no such field, and my tests used fakes that did. The guard failed closed, so no data went astray. A test now creates a workspace with T2's own code and runs it on the object store. The lake takes the org from the workspace folder, and any other folder shape still refuses.
- My object store tests only passed on a fresh Postgres. Catalog names are fixed, so a second run found rows from the first, pointing at a bucket that no longer existed. CI always starts fresh and would never have shown it. Each test now gets a throwaway schema.
