# Sketch 06: The connector ladder

## Purpose

Any source, one landing path. Every rung reuses the declarative source
spec and the same bronze/WAP road; a rung ships only when it moves real
bytes on a laptop behind a replayable fixture and a contract test. No
rung is listed in Sources until then; the map's no-posing test extends to
connectors.

## The rungs, cheapest-true first

1. **Drops** (src_drops): SFTP and object stores (S3/GCS/R2). Same four
   file kinds, remote `open()`. Fixture: a local sftp container script
   plus a MinIO bucket seeded by the fixture, both replayable offline.
2. **Warehouses** (src_jdbc): ADBC/Arrow readers for Postgres first (we
   can fixture it honestly), then SQL Server, Oracle, Snowflake,
   BigQuery as fixtures become possible; a rung per driver, each with a
   schema-drift contract test.
3. **SAP / ERP** (src_sap): IDoc file drops first; an IDoc is a
   fixed-width cousin and lands through the copybook path with a new
   layout dialect. OData extractors second. RFC/BAPI adapter last, and
   only against a real system someone owns.
4. **Streams** (src_stream): Kafka-protocol consumer, batch windows into
   bronze with the window id as run id; replayable fixture = a recorded
   topic segment.
5. **CDC** (src_cdc): Debezium-class capture mapped to event sources.
   After streams, never before.

## The per-rung contract (every rung, no exceptions)

- A source spec example in the docs, a fixture that replays offline, a
  contract test (schema, nulls, encodings, truncation), bronze rows
  landing through WAP with checks, and the Sources view listing it with
  its protocol. Benchmarks published only if claimed.
- Credentials: env vars documented per rung, never stored by us; the
  local-first promise (bytes stay on the machine) holds for every rung.

## Gate (ladder entry, rung 1)

Done when: a retail-shaped CSV lands from a local MinIO bucket through
the same contract + WAP + quarantine path as a local file, byte-identical
bronze versus the local-file run on the same fixture, by test.

## Not on the ladder

Third-party SaaS app connectors (OAuth + review + maintenance tax, the
anti-$0 shape); anything that uploads user data to us.
