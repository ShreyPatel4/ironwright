// What the Worker does in front of the container, kept apart from the Cloudflare runtime so
// test.mjs can run it under node with a fake container.
//
// The path is forwarded as it came. Behind coconutlabs.org's rewrite it starts with
// /projects/ironwright/app, at the root of workers.dev it does not, and the app answers both
// (IRONWRIGHT_BASE_PATH). So nothing is stripped here.

export const STARTING_RETRY_SECONDS = 5;

// The container's command. The API and the job worker run in one instance, because they share
// the data root (C1 keeps it on R2). If either exits, the container exits and the next request
// starts both again. The demo packs are built from code on a fresh disk, after the API is up;
// that build is not waited on, so its end never stops the container.
export const START = [
  'trap "kill 0" TERM',
  "ironwright worker & worker=$!",
  "ironwright serve-platform & api=$!",
  "(test -e \"$IRONWRIGHT_DATA/.demo-packs-built\" || (ironwright run --pack all && touch \"$IRONWRIGHT_DATA/.demo-packs-built\")) >/tmp/demo-packs.log 2>&1 &",
  'wait -n "$worker" "$api"',
  "exit 1",
].join("\n");

// Set only when the app did not. The app sends its own CSP; a second one would intersect.
const EDGE_HEADERS = {
  "strict-transport-security": "max-age=31536000",  // this app only: never policy for the whole zone
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "referrer-policy": "no-referrer",
  "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
};

function secured(response) {
  const out = new Response(response.body, response);
  for (const [k, v] of Object.entries(EDGE_HEADERS)) {
    if (!out.headers.has(k)) out.headers.set(k, v);
  }
  return out;
}

const STARTING_PAGE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="${STARTING_RETRY_SECONDS}">
<title>Ironwright is starting</title>
<style>
  :root { --bg: #F7F7F5; --ink: #0B0B0C; --ink-1: #55555A; --rule: #E2E2DE; --accent: #2F5BEA; }
  @media (prefers-color-scheme: dark) { :root { --bg: #0E0E10; --ink: #F2F2F0; --ink-1: #A0A0A6; --rule: #2A2A2E; } }
  body { background: var(--bg); color: var(--ink); font: 15px/1.6 Geist, ui-sans-serif, system-ui, sans-serif;
         margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 0 16px; }
  .panel { border: 1px solid var(--rule); border-radius: 6px; max-width: 420px; padding: 20px 22px; }
  .mark { font: 12px ui-monospace, monospace; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-1); }
  .bar { height: 2px; background: var(--rule); border-radius: 2px; overflow: hidden; margin-top: 16px; }
  .bar span { display: block; height: 100%; width: 30%; background: var(--accent);
              animation: slide 2.4s cubic-bezier(.4, 0, .2, 1) infinite; }
  @keyframes slide { from { transform: translateX(-100%); } to { transform: translateX(340%); } }
  @media (prefers-reduced-motion: reduce) { .bar span { animation: none; width: 100%; } }
</style>
</head>
<body>
<div class="panel" role="status">
  <div class="mark">ironwright</div>
  <h1 style="font-size: 20px; margin: 8px 0 6px">Starting up</h1>
  <p style="color: var(--ink-1); margin: 0">The server was asleep and is waking now. This takes
  about a minute. The page checks again every ${STARTING_RETRY_SECONDS} seconds, so you can leave it open.</p>
  <div class="bar"><span></span></div>
</div>
</body>
</html>
`;

function starting(request) {
  const headers = { "retry-after": String(STARTING_RETRY_SECONDS), "cache-control": "no-store" };
  const wantsPage = (request.headers.get("accept") || "").includes("text/html");
  if (wantsPage) {
    return new Response(STARTING_PAGE, { status: 503, headers: { ...headers, "content-type": "text/html; charset=utf-8" } });
  }
  return new Response(JSON.stringify({ status: "starting", retry_after: STARTING_RETRY_SECONDS }),
                      { status: 503, headers: { ...headers, "content-type": "application/json" } });
}

// stub is the one container instance: getState(), wake(), fetch(request).
export async function handle(request, stub, ctx) {
  let state = "stopped";
  try { state = (await stub.getState()).status; } catch { /* treat as asleep */ }
  if (state !== "healthy") {
    // Wake it in the background and answer now, so nobody stares at a hung request.
    ctx.waitUntil(Promise.resolve().then(() => stub.wake()).catch(() => {}));
    return secured(starting(request));
  }
  try {
    return secured(await stub.fetch(request));
  } catch {
    return secured(starting(request));
  }
}
