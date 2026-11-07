// ironwright-platform: one Worker on workers.dev in front of one container.
// coconutlabs.org/projects/ironwright/app/* reaches it through a Vercel rewrite.
// The runbook is deploy/cloudflare/README.md.
import { Container } from "@cloudflare/containers";
import { START, handle } from "./edge.js";

// Set with `wrangler secret put`, never in a file. Each one is handed to the container.
const SECRETS = [
  "IRONWRIGHT_SECRET_KEY",
  "IRONWRIGHT_DB_URL",
  "IRONWRIGHT_S3_ENDPOINT",
  "AWS_ACCESS_KEY_ID",
  "AWS_SECRET_ACCESS_KEY",
  "IRONWRIGHT_METRICS_TOKEN",
  "ANTHROPIC_API_KEY",
];

// Plain settings from wrangler.jsonc "vars".
const VARS = [
  "IRONWRIGHT_ENV",
  "IRONWRIGHT_BASE_PATH",
  "IRONWRIGHT_CONSOLE_ORIGIN",
  "IRONWRIGHT_SIGNUP",
  "IRONWRIGHT_DEMO_ORG",
  "IRONWRIGHT_STATE_URI",
  "IRONWRIGHT_LAKE_URI",
  "AWS_REGION",
];

export class IronwrightPlatform extends Container {
  defaultPort = 8000;
  // A demo walk is minutes with pauses. An hour idle before sleep means no cold start mid walk.
  sleepAfter = "60m";
  entrypoint = ["bash", "-c", START];

  constructor(ctx, env) {
    super(ctx, env);
    const pass = {};
    for (const name of [...VARS, ...SECRETS]) {
      if (env[name]) pass[name] = env[name];
    }
    this.envVars = {
      ...pass,
      IRONWRIGHT_HOST: "0.0.0.0",
      IRONWRIGHT_I_KNOW_THIS_IS_EXPOSED: "1",
      IRONWRIGHT_RUNS_WAIT: "0",
      IRONWRIGHT_DATA: "/data",
    };
  }

  // Called by the edge when the container is not healthy. The wait lives here, in the
  // Durable Object, so it outlasts the request that asked.
  async wake() {
    this.ctx.waitUntil(this.startAndWaitForPorts({ cancellationOptions: { portReadyTimeoutMS: 300_000 } }));
  }
}

export default {
  async fetch(request, env, ctx) {
    return handle(request, env.IRONWRIGHT.getByName("main"), ctx);
  },
};
