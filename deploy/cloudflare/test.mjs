// The Worker's edge logic with a fake container behind it. No network, no wrangler.
//   node deploy/cloudflare/test.mjs
import assert from "node:assert/strict";
import { handle, STARTING_RETRY_SECONDS } from "./src/edge.js";

function fakeStub(status, answer = () => new Response("from the app", { status: 200 })) {
  const seen = { started: 0, forwarded: [] };
  return {
    seen,
    async getState() { return { status }; },
    async wake() { seen.started += 1; },
    async fetch(request) { seen.forwarded.push(request); return answer(request); },
  };
}

const ctx = () => {
  const waits = [];
  return { waits, waitUntil: (p) => waits.push(p) };
};

const page = (path, accept = "text/html") =>
  new Request(`https://ironwright-platform.example.workers.dev${path}`, { headers: { accept } });

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

test("a healthy container gets the request as it came, prefix and all", async () => {
  const stub = fakeStub("healthy");
  const r = await handle(page("/projects/ironwright/app/console/assets/a.js?v=1"), stub, ctx());
  assert.equal(r.status, 200);
  assert.equal(new URL(stub.seen.forwarded[0].url).pathname, "/projects/ironwright/app/console/assets/a.js");
  assert.equal(new URL(stub.seen.forwarded[0].url).search, "?v=1");
});

test("the root of the workers.dev host is forwarded too", async () => {
  const stub = fakeStub("healthy");
  await handle(page("/readyz", "application/json"), stub, ctx());
  assert.equal(new URL(stub.seen.forwarded[0].url).pathname, "/readyz");
});

test("every answer carries the edge security headers", async () => {
  const r = await handle(page("/console/"), fakeStub("healthy"), ctx());
  assert.match(r.headers.get("strict-transport-security"), /max-age=\d+/);
  assert.doesNotMatch(r.headers.get("strict-transport-security"), /includeSubDomains/i, "not ours to set for the zone");
  assert.equal(r.headers.get("x-content-type-options"), "nosniff");
  assert.equal(r.headers.get("x-frame-options"), "DENY");
  assert.ok(r.headers.get("permissions-policy"));
});

test("the edge never adds a second CSP: two policies intersect", async () => {
  const csp = "default-src 'self'";
  const r = await handle(page("/console/"), fakeStub("healthy", () =>
    new Response("ok", { headers: { "content-security-policy": csp } })), ctx());
  assert.equal(r.headers.get("content-security-policy"), csp);
  const bare = await handle(page("/console/"), fakeStub("healthy"), ctx());
  assert.equal(bare.headers.get("content-security-policy"), null);
});

test("an app header wins over the edge's", async () => {
  const r = await handle(page("/x"), fakeStub("healthy", () =>
    new Response("ok", { headers: { "x-frame-options": "SAMEORIGIN" } })), ctx());
  assert.equal(r.headers.get("x-frame-options"), "SAMEORIGIN");
});

test("a sleeping container is woken, and a person gets the calm page with a retry", async () => {
  const stub = fakeStub("stopped");
  const c = ctx();
  const r = await handle(page("/projects/ironwright/app/console"), stub, c);
  assert.equal(r.status, 503);
  assert.equal(r.headers.get("retry-after"), String(STARTING_RETRY_SECONDS));
  assert.equal(r.headers.get("cache-control"), "no-store");
  const html = await r.text();
  assert.match(html, /Starting up/);
  assert.match(html, new RegExp(`http-equiv="refresh" content="${STARTING_RETRY_SECONDS}"`));
  assert.doesNotMatch(html, /—/, "no em dashes in UI copy");
  assert.equal(c.waits.length, 1);
  await Promise.all(c.waits);
  assert.equal(stub.seen.started, 1);
  assert.equal(stub.seen.forwarded.length, 0);
});

test("an API call while starting gets JSON it can retry, not a page", async () => {
  const r = await handle(page("/projects/ironwright/app/workspaces", "application/json"), fakeStub("running"), ctx());
  assert.equal(r.status, 503);
  assert.equal(r.headers.get("content-type"), "application/json");
  assert.deepEqual(await r.json(), { status: "starting", retry_after: STARTING_RETRY_SECONDS });
});

test("readyz says starting while the container boots", async () => {
  const r = await handle(page("/readyz", "*/*"), fakeStub("stopped"), ctx());
  assert.equal(r.status, 503);
  assert.equal((await r.json()).status, "starting");
});

test("a container that will not answer yet is a starting page, not a 500", async () => {
  const stub = fakeStub("healthy", () => { throw new Error("connection refused"); });
  const r = await handle(page("/console/"), stub, ctx());
  assert.equal(r.status, 503);
  assert.match(await r.text(), /Starting up/);
});

let failed = 0;
for (const [name, fn] of cases) {
  try { await fn(); console.log(`ok  ${name}`); }
  catch (e) { failed += 1; console.log(`FAIL  ${name}\n  ${e.stack}`); }
}
if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log(`${cases.length} passed`);
