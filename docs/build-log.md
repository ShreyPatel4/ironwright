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
## 18. A key edit could pick what the proposer never would

**Symptom.** Reading the first golden proposal, I saw `premium` marked unique. A new test confirmed that a `set_key` edit took it, and took `loss_date` too.

**Cause.** The proposer refuses measures and dates as keys, but the edit path only asked if the column was unique. Three policies with three premiums are unique today and collide on the next load. The same rule lived in one of the two places that needed it.

**Fix.** `set_key` applies the same type rule, with a test for each. Lesson: a human edit goes through the rules a proposal goes through. Approving something does not make it valid.

## 19. A typo that read as a full org

**Symptom.** With every member seat taken, inviting `not an email` answered 429 instead of 422.

**Cause.** The seat check runs before the invite does, and it compared the raw text to the seats. The
invite is what validates an email, so it never got to speak. The admin would be told to free a seat to
fix a typo.

**Fix.** The seat check cleans the email the same way the invite does, and steps aside when it cannot, so
the invite refuses it in its own words. While I was there I took out `pydantic` on `PUT /llm-key` and put
it back to see what it does: its 422 echoes the body, which here is a live key. That route reads its own
body, and the leak test sends the three shapes that echo.

## 20. Two doors that opened after I checked

**Symptom.** My PR said Ask was the only model call a tenant could reach, and that runs had two doors.
Both were true when I wrote them. Rebasing onto the ontology proposer made both false, and every test
stayed green.

**Cause.** The proposer's job builds its own `LLM()` when asked to narrate, so a member could queue it
over and over on the platform key with no limit, and past an org's own key. Accepting a proposal queues a
run straight on the store, so it never met the runs limit. Nothing in my suite could notice a new call site.

**Fix.** Both now go through `quotas`. Out of tokens, the proposal is still made and the paragraph is left
out. Out of runs, accept refuses before it writes. The lesson: grep for `LLM(` and `.enqueue(` on every
rebase, because a limit only holds at the doors it knows about.

## 21. A settings table that named my home directory

**Symptom.** The new security tests went red in two places I had assumed were fine. Any tenant could
run `select * from duckdb_settings()` on the locked connection and read `secret_directory`, which
holds the server's home path. And `orgpolicy.read_org("..")` read an `org.json` above the data root.

**Cause.** Locking DuckDB's configuration stops a query changing a setting. It does not stop a query
reading one, and I had blanked only the temp directory. The traversal was hidden by the app gate,
which refuses `..` first, so nothing tested the module on its own. The escalation shape checks had a
smaller miss: `$` with `re.match` lets a trailing newline through.

**Fix.** `apply_limits` blanks every directory setting, both modules go through `safe_child`, and the
shape checks use `fullmatch`. Lesson: "locked" and "hidden" are different promises, and a gate in
front of a module is not a test of the module.

## 22. A fast worker skipped the step I wanted you to read

**Symptom.** The wizard test uploaded two files and waited for the Profile heading. It got Map. Some runs passed and most failed.

**Cause.** After an upload the wizard asked the server which step comes next. That is the right question on a resume and the wrong one here. When the worker finished the profile before the reload came back, the server's honest answer was Map, so you never saw the types, keys and joins you were meant to check.

**Fix.** Upload now names the step it wants, and every other step still asks the server. Lesson: "the furthest step you could be on" and "the step you should look at now" are different questions. They only look the same when the worker is slow.

## 23. Every slice was green and the journey did not connect

**Symptom.** The first walk of the whole journey stopped at step three. T1, T3 and T4 had each passed
their own tests, and a customer still could not get from an upload to an answer.

**Cause.** Each break sits between two slices, where no test of either one looks. `POST /jobs` answers
404 for a workspace that does not exist yet, and a profile job runs before one can. Nothing over HTTP
calls `create_workspace`. `GET /workspaces` never lists a tenant workspace. `_pack()` answers 404 for
one, so Ask cannot reach it. A stub profile handler and a fixed width `header` key were on that list too,
until T3 fixed both.

**Fix.** Not mine to make, since these files belong to other slices. Each break is now a strict xfail
in `tests/e2e/test_journey.py` that names who it waits on, and goes red the day it is fixed. One already
did: T3's fix for the `header` key turned its test red at my rebase. Lesson: a contract between two
slices needs a test that crosses it, so walk the whole path early.


## 24. A slug decided whose rules a tenant's Ask ran on

**Symptom.** W2 reported a `live_engine` hazard on Ask. A test filed rules under a tenant's slug at the data root: a forbid on every read and a permit on every action. The tenant's engine picked them up.

**Cause.** `live_engine(name)` reads generated policy and escalation grants from `data_root/<name>`, one namespace across every org. It was built for packs and scenario orgs, where the name is unique. A tenant slug is unique only inside its org. The caller also swallowed every exception, so a failure left the request on whatever engine it already had.

**Fix.** A tenant passes its pack. It gets the rules in its own folder plus a default read for people, and it never touches the shared namespace. Errors there are no longer swallowed. Lesson: a name is an identifier only inside the scope that made it unique.

## 25. Making a workspace over HTTP would have opened another org's scenario

**Symptom.** Writing `POST /workspaces` for a tenant, I read the gate and tried it with the guard off. Org B took the slug `o<org A's 8 hex>-north`, the spelling of org A's scenario folder. B then read A's organization and deleted it. Both answered 200, and A got a 404 on its own data.

**Cause.** `can_see_workspace` says yes as soon as the org holds a folder with that name, and the scenario routes then read the shared folder by the same name. Nothing could reach this while tenant workspaces were only made behind the API.

**Fix.** The route refuses that spelling, demo pack names and any existing scenario folder, in one answer that does not say whether the folder exists. The gate itself is T1's, so the deeper fix is a cross-ticket ask on the PR. Lesson: a new way to create a name is a new way to collide with every rule that trusts names.

## 26. Two runs inside one second lost every object

**Symptom.** My test re-proposed an ontology after a rebuild and got "no object named Customer". A near copy of that test passed.

**Cause.** A run id is the clock to the second, and a bronze view keeps only the newest id. Two runs inside a second share an id, so the view holds each row twice, no column is unique, and the proposal finds no keys. Small files really do run this fast. I first blamed bronze keeping every run, and my strict xfail on that story passed by chance on its first outing.

**Fix.** `lake.py` is outside my ticket, so a frozen clock pins it with a strict xfail, and the fix is a cross-ticket ask. Lesson: when a test passes in one runner and fails in another, look for a clock before writing down a cause.

## 27. A refresh credited the wrong file with the run

**Symptom.** A new test uploaded the next extract while a run was going. The run loaded v1, and when it finished, v1 had no runs and v2 had one. v2's bytes were never in bronze.

**Cause.** I recorded the run against the newest version once the run ended. A run can take minutes, and the next file does not wait for it. So asking for v2's rows would have handed back v1's, with no error anywhere.

**Fix.** A run belongs to the version whose upload the pack actually loaded, found by its upload ref. The drift gate, the rebuild, the record and the version compare all use that one answer. Lesson: "latest" is a question about a moment, so pin it when the work starts.

## 28. The demo gate fails six hours a day where I sit

**Symptom.** `make demo-check` went BROKEN on "a grant is policy the serving engine consults". Clean main failed the same way on this laptop while main's CI was green.

**Cause.** The script grants until tomorrow by `date.today()`, which is local. A grant dies at the start of that date in UTC. At 20:53 on this laptop, UTC is already on the next day, so the grant was dead the moment it was made. CI runs in UTC and can never see it.

**Fix.** Not mine to make, since `scripts/demo_console_check.py` is outside my ticket. I ran the gate with `TZ=UTC`, and the fix is a cross-ticket ask: take the date from UTC. Lesson: a gate that only passes in CI's timezone is a clock test nobody meant to write.

## 29. Every route read as unmatched

**Symptom.** My first request line said `route: unmatched` for `/jobs`, and the requests counter had no `/jobs` series at all. The status, the ids and the timing were all right.

**Cause.** Two separate walls. `BindPrincipalHeader` copies the ASGI scope when sessions are on, so `scope["route"]` and `request.state.ctx` never come back out to a middleware outside it. That one only shows with the door closed, which is why these tests run with it closed. Then, when I matched routes myself, FastAPI 0.143 turned out to keep each included router behind a single object whose `matches()` says yes and names no template.

**Fix.** The middleware flattens the routes itself, the way the isolation sweep does, and matches compiled templates. It reads the session itself, before the app runs, so a logout still logs who asked. That is one more session lookup per request with a token. I asked for a cheaper way in the PR.

## 30. A crash could quote a cell into the server's log

**Symptom.** I wrote a test that a crash logs its type and not its message. It passed, and it proved nothing: my new route sat behind the gate, so the test got a 401 and the handler never ran. With a session it reached the handler, and I saw what my middleware would have done next, which is raise.

**Cause.** Raising hands the exception to uvicorn, which logs the traceback with the message. My JSON line was clean and the line beside it was not. The worker had the same problem: `traceback.print_exc` on its loop, and a SQLAlchemy error prints the parameters it failed on, including a job's error text.

**Fix.** The middleware answers the 500 itself, with the request id, and logs the type plus file and line. So does the worker. The formatter also writes any exception passed as a log argument as its type, which covers `sqlguard` logging raw DuckDB errors without touching it. The lesson: a leak test has to prove its canary reached the leak point first, so these tests now assert that before they scan.

## Smaller ones

- A session-scoped Playwright broke four MCP and upload tests in other files. It keeps an event loop running on the main thread, so their `asyncio.run()` refused to start. Each file passed alone. The browser now lives only as long as its own module.
- A `month` column broke DuckDB parsing (reserved word). Renamed to `ym`.
- The ontology build took 7 seconds with row inserts. Arrow bulk inserts brought it to about 2.
- The audit hash check failed after a type round trip. Hashing a canonical JSON of the values fixed it.
- A LangGraph interrupt was logged as an error span. It's now an `interrupt` event, because pausing for a human is not a failure.
- The new link filter read customer emails as links. Scanning every string cell of every pack through the guardrail found it; no narration happened to carry one, so no eval would have. The bare domain pattern now skips anything after an `@`.
- My hosted entrypoint built the platform app straight, so it served without S5's hardening: no security headers, no origin guard, no login limit. S5 installs that in `cli._serve`, and a second road to uvicorn skipped it. `tests/test_hosted.py` now checks the headers and the origin guard on the hosted app.
- The first compose run could not pull MinIO. Docker Hub has dropped the repos and `dl.min.io` answers 410: the open-source project is archived with no security updates. The image pin looked fine in review and only a real pull found it. Compose now runs Versity's S3 gateway, and the bucket init uses our own image, so `mc` is gone too.
- The same CI job piped pytest into `tee` under `bash -e` with no pipefail, so failing tests would have read as green. The job now states `shell: bash`, which adds pipefail.
- Every real tenant run refused to start on the hosted lake. My guard read `org_id` off the pack, T2's `Pack` has no such field, and my tests used fakes that did. The guard failed closed, so no data went astray. A test now creates a workspace with T2's own code and runs it on the object store. The lake takes the org from the workspace folder, and any other folder shape still refuses.
- The refresh gate reworded a failure the wizard shows. It stops a run before the loader can say `the file has no column column_6`, and my words were different, so W1's test went red at rebase. The gate now uses the loader's words.
- X1 and I hit the same bug from two sides, so #26 is fixed for tenant runs. X1's strict xfail went red at rebase because my `_next_run_id` moves past a repeated second. I reverted that line to check, saw it xfail again, and took the marker off. Demo packs still use the one second id.
- My new drift routes answered "no such resource" past the gate, which is the gate's own words. The isolation sweep stopped it: if a route 404s everyone in those words, a broken gate looks like a working one. They now say why in their own words.
- My object store tests only passed on a fresh Postgres. Catalog names are fixed, so a second run found rows from the first, pointing at a bucket that no longer existed. CI always starts fresh and would never have shown it. Each test now gets a throwaway schema.
- My first default deny test crashed on `props->>'status' = 'OPEN'`. DuckDB tried to cast the whole props JSON to a number and raised a conversion error. Writing `(props->>'status') = 'OPEN'` fixed it.
- The insurance_policy Insured kept `match_key` unmasked, because I had called it a digest and moved on. It is an md5 of a birth date and four digits, which is small enough to brute force, and `agent:coverage-triage` could read it. The masking test went red once it asked for that field. `match_key` is now listed as sensitive with the other two.
- My first plan for org delete would have left a tenant behind on the hosted stack. A hosted lake's Iceberg catalog sits in the metadata store, in pyiceberg's own tables, keyed by a name holding the org id, under no `org_id` column. A list of known tables would never find it. So the delete and its test both read the live schema, and that test asserts the org's rows are there first, or it proves nothing.
- Any job kind you register is one a member can start through `POST /jobs`, which is member level and checks only the workspace. Registering `org_delete` would have let any member delete the org. Tests written first caught this: the job does nothing unless the owner route wrote the deleting mark, and export reads the caller's role itself. Check this whenever a new kind lands.
- An invite accepted while its org was being marked for deletion still wrote a member and a working session, because the accept had read the invite before the mark committed. A test that commits the mark at that exact moment went red on both stores. The accept now looks again under the org's lock.
- The wizard had drifted off nearly every route it calls, and 200 green tests said otherwise. `POST /jobs` 404ed the profile, `GET /uploads` did not exist, Map sent a body the real `POST /workspaces` refuses, the journey looked for the profile job under the wrong label, and `GET /ontology` 404ed every tenant workspace, so nobody could ever reach Ask. The suite's adapters stood in for routes that did not exist yet, then outlived them, and they were replacing the real ones. Its browser tests also skip in the CI job that has no browser. A new test walks the wizard against `ironwright platform` and `ironwright worker` as real processes with nothing stubbed, and it runs where a skip fails the job.
- The first run of a file whose header ends in a comma stopped on drift against its own spec. The profiler named the empty column `column_6`, the reader named it `col_6`, and the gate reported the difference as a missing column. Both now ask one function and leave that column out, and the proposal says why. A first run has no baseline, so it has nothing to drift from: a spec that does not fit the file now fails in the loader's words.
- The same split hit any `date`, `month` or `orderId` header: the draft said `date_` or `order_id` and bronze said `date` or `orderid`. A profiler test pinned the rename because a `month` column once broke DuckDB parsing, so I walked such a file to a cited answer before changing it. A delimited draft now carries the names bronze reads.
- A refresh that failed could still show an empty inbox. Approvals drew "Inbox empty" before its first read came back and after any read failed, and the audit log drew an empty table. Fixing that was not enough: a test that went offline mid-load stayed red, because the first read landed after the failed refresh and painted over it. Only the newest read may speak now.
- The demo film's first take captioned "objects and links" over an ontology with no links. The rules picked `client_key` for the agency book, so the billing file's `policy_nbr` had nothing to point at. The beat now has a person correct the key, and it fails if the proposal holds no link. A caption that claims something needs a check behind it.
- The product review found the wizard dead at step 2 on a real server while its browser test was green. `_install_gaps` in the test swapped in the routes the wizard wanted, and `journey.py` walks the API without loading the console, so nothing ran the two together. That one is #22. The fix there has to delete the stand-ins, or it can come back.
- `make demo-check` failed every evening west of UTC. The script built tomorrow in local time, and a grant dies at the start of its date in UTC, so at 22:00 in Chicago the grant was already dead. CI runs in UTC and could never see it.
- A refused upload spent one of the day's 20. The door spends before the bytes arrive, which is right for the limit, and nothing gave it back when the file turned out to be a PDF.
