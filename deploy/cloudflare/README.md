# Ironwright on Cloudflare Containers

This is how I put the hosted product at `coconutlabs.org/projects/ironwright/app/`.

```mermaid
flowchart LR
  B["Browser<br/>coconutlabs.org/projects/ironwright/app/..."] --> V["Vercel<br/>next.config.ts rewrite"]
  V -- "same path" --> W["Worker ironwright-platform<br/>shrey77-wrk.workers.dev<br/>security headers, starting page"]
  W -- "getByName(main)" --> C["Container, one instance<br/>ironwright serve-platform<br/>+ ironwright worker"]
  C -- "TLS" --> N[("Neon Postgres<br/>tenancy, jobs,<br/>Iceberg catalog")]
  C -- "S3 API" --> R[("R2 ironwright-state<br/>lake + durable state")]
```

The coconutlabs.org zone is DNS only and Vercel serves the site, so a Worker route would never
fire. The site rewrites the path to the Worker on workers.dev instead, the same way `/live` works.
The path arrives at the container unchanged, and `IRONWRIGHT_BASE_PATH` lets the app answer it.
The same container also answers at the root of the workers.dev host, which is handy for smoke tests.

One instance runs the API and the job worker together, because they share the data root. The
container disk is wiped on sleep or restart, so C1's durable state (`IRONWRIGHT_STATE_URI`) keeps
tenant files on R2, Neon keeps the metadata, and the demo packs are rebuilt from code at boot.
The container sleeps after an hour idle. The next visitor sees a calm "starting up" page that
checks again every few seconds, for about a minute.

## The checklist, in order

Everything below runs from `deploy/cloudflare/` on my laptop. Nothing here is automated, on purpose.

1. **Log in.** `npm install`, then `npx wrangler login`, then `npx wrangler whoami`. Note the account id.
2. **Workers Paid.** Containers need it. Dashboard, Workers and Pages, Plans. Check with
   `npx wrangler containers list` (it errors on the free plan).
3. **Neon.** Create a project `ironwright` in a US East region. Copy the pooled connection string.
   It looks like `postgresql://USER:PASSWORD@ep-xxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require`.
   The container connects to it directly over TLS. Hyperdrive only helps Worker code, so I skip it.
4. **R2.** `npx wrangler r2 bucket create ironwright-state`. Then R2, Manage API tokens, create an
   Object Read and Write token scoped to `ironwright-state`. Keep the access key id, the secret and
   the S3 endpoint `https://<account_id>.r2.cloudflarestorage.com`.
5. **Secrets.** Each one prompts for the value, so nothing lands in shell history:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # the key below
   npx wrangler secret put IRONWRIGHT_SECRET_KEY
   npx wrangler secret put IRONWRIGHT_DB_URL
   npx wrangler secret put IRONWRIGHT_S3_ENDPOINT
   npx wrangler secret put AWS_ACCESS_KEY_ID
   npx wrangler secret put AWS_SECRET_ACCESS_KEY
   npx wrangler secret put IRONWRIGHT_METRICS_TOKEN
   ```

   `ANTHROPIC_API_KEY` is optional. Without it the narration stays offline and answers still cite evidence.
   Plain settings (prod, the prefix, invite only signups, the demo org, the bucket URIs) are in
   `wrangler.jsonc` under `vars`. A prod container refuses to boot without the secret key, the
   database, the state URI and the lake URI.
6. **Deploy.** `npm test && npx wrangler deploy`. The first deploy builds the repo `Dockerfile` and
   pushes it, which takes a few minutes. The output names the URL,
   `https://ironwright-platform.shrey77-wrk.workers.dev`.
7. **The site.** Apply `coconutlabs-site.patch` in the coconutlabs-site checkout
   (`git apply --check` first), run its gates (`npm run test:all`), and push it the way that repo
   ships. That adds the two rewrites and the "Try it" link.

## Smoke

The first request wakes the container, so I give it a few minutes:

```bash
python deploy/hosted_smoke.py --base https://ironwright-platform.shrey77-wrk.workers.dev \
    --base-path /projects/ironwright/app --wait 300
python deploy/hosted_smoke.py --base https://coconutlabs.org --base-path /projects/ironwright/app --wait 300
```

Signups are invite only in prod, so the smoke walks what a visitor can do: health, the console
page, the door closed, signup refused, the demo door opening a viewer seat that reads a demo pack
and cannot write. The full tenant walk runs locally with `make cf-check`, and the GitHub workflow
`deploy-cloudflare.yml` runs both the deploy and this smoke when I start it by hand.

## Rollback

```bash
npx wrangler deployments list
npx wrangler rollback <deployment-id>
```

A rollback moves the Worker and the container image back together. Neon and R2 are untouched by
it, so data written since stays. If a schema change is the problem, Neon's branch restore takes
the database back to a point in time. To take the product down entirely, remove the two rewrites
from the site (the marketing page stays), or `npx wrangler delete ironwright-platform`.

## Limits I know about

| Limit | Why |
|---|---|
| One instance | Two would split the data root. RPO is the last completed operation (C1). |
| Cold start about a minute | The image boots, then the demo packs rebuild in the background. |
| Door rate limits are shared by everyone | Every request reaches the app from the Worker, so the limiter sees one peer: ten tries a minute per door, the demo door included, across all visitors. |
