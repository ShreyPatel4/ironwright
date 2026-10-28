# BI over the mirrored lake

I point an external BI tool at a postgres mirror of the lakehouse. The
Iceberg catalog is local and no BI tool reads it directly, so the engine
copies every silver and gold table into a postgres schema and verifies
each copy row for row. The BI tool then sees plain postgres tables named
`silver_*` and `gold_*`.

## What is proven and what is manual

Tests prove the mirror. `tests/test_bi.py` runs against a real postgres,
checks that every silver and gold table round-trips with identical rows,
columns and values, and checks that a planted corruption flips the verdict
to false. Without `POSTGRES_URI` those tests skip and say so.

The BI tool itself is manual. Metabase setup, the questions you build and
the dashboard you share are your work inside Metabase. I do not automate
or test any of it. Ironwright claims the mirror and the iframe that shows
your dashboard URL, nothing more.

## The recipe

1. Start postgres and Metabase:

   ```bash
   cd deploy/bi
   docker compose up -d
   ```

2. Mirror a workspace. From the console, open the BI embed view and click
   "Mirror lake to postgres". Or call the API:

   ```bash
   export POSTGRES_URI=postgresql://postgres:iw@127.0.0.1:54329/postgres
   curl -X POST localhost:8000/workspaces/retail/bi/mirror \
     -H 'content-type: application/json' -d '{"schema": "iw_retail"}'
   ```

   The response lists every mirrored table with its row count and a
   `match` verdict from reading the copy back. Leave the body empty and
   the server mints a throwaway schema instead.

3. Connect Metabase to the mirror. Open http://localhost:3000, finish the
   first-run setup, then add a PostgreSQL database. From inside the
   Metabase container the server is `postgres`, port `5432`, user
   `postgres`, password `iw`, database `postgres`. Limit the schemas to
   the one you mirrored.

4. Build a dashboard in Metabase, enable public sharing on it, and paste
   the public URL into the BI embed view. Only an absolute http(s) URL is
   accepted; `javascript:`, `data:` and relative paths are rejected at
   save. The console renders the URL in a sandboxed iframe.

## Notes

- The mirror is a copy, not CDC. Rerun it after a pipeline run to refresh
  the postgres side; each rerun drops and recreates the mirrored tables.
- The same postgres serves the test suite:
  `POSTGRES_URI=postgresql://postgres:iw@127.0.0.1:54329/postgres uv run pytest tests/test_bi.py -q`.
- Looker and Power BI work the same way: point them at the mirrored
  schema over postgres, publish, and paste the embed URL. I ship no
  vendor SDK and claim no vendor integration.
