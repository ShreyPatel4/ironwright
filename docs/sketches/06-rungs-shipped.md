# Sketch 06 follow-up: connector rungs shipped

Five rungs from the connector ladder now exist as source kinds that
`legacy.read_source` dispatches to `ironwright/connectors.py`. Every rung
moved real bytes on this laptop behind a replayable offline fixture with a
contract test in `tests/test_connectors.py`. Where a local-file comparison
exists, bronze is asserted byte-identical (same schema, same rows, same
Arrow IPC bytes). Nothing below is listed in Sources beyond what the tests
prove.

## Rung 1: object-store drops (kind `s3`)

- Proven: a retail-shaped CSV drop and a fixed-width mainframe file land
  from an S3 bucket byte-identical to the local-file path on the same
  bytes. Objects download to a temp dir and go through the exact local
  reader, so identity holds by construction and by test.
- Fixture: moto's in-process `mock_aws`, bucket seeded inside the test
  from local files. Fully offline, replayable.
- Not claimed: SFTP, GCS, R2, real network endpoints, unsigned public
  buckets (the `anon` and `endpoint_url` params exist but only the moto
  path is fixture-proven).

## Rung 2: warehouses via ADBC (kind `adbc`)

- Proven: the sqlite driver. Same db, same query, read through ADBC,
  byte-identical bronze versus the native `sqlite` kind. Nulls, empty
  strings, ints, floats covered.
- Fixture: a sqlite db built inside the test. Offline, replayable.
- Not claimed: postgresql. The driver code path exists and its test runs
  only when `POSTGRES_URI` points at a real server; until it has an
  offline fixture it stays out of the Sources listing. SQL Server,
  Oracle, Snowflake, BigQuery are not written at all.

## Rung 3: SAP IDoc flat files (kind `idoc`)

- Proven: a synthetic but shape-true MATMAS-ish flat file (EDI_DC40
  control record first, fixed-width records, segment name in the first 30
  chars, E2MARAM data segments) round-trips: every planted field value
  lands, a truncated last record parses its leading fields and nulls the
  tail, and an unknown segment is never dropped, it lands with
  `_segment` set and the raw payload in `_raw`. One table out, rows
  tagged with `_segment`, strings in bronze.
- Fixture: a generator inside the test emits the file; values invented,
  clearly synthetic. Offline, replayable.
- Not claimed: OData extractors and RFC/BAPI. Flat-file IDocs only.

## Rung 4: stream windows from recorded segments (kind `stream_window`)

- Proven: committed synthetic jsonl segments land with deterministic
  `_window_id` (UTC ISO window start). Events exactly on a boundary go to
  the later window, asserted on two boundary events. Re-running is
  idempotent-identical to the IPC byte. `PT5M` and `300` give the same
  windows. An event missing its timestamp field is an error, never a
  silent drop.
- Fixture: `tests/fixtures/stream/segment-00*.jsonl`, committed. Offline,
  replayable.
- Not claimed: a live Kafka-protocol consumer, consumer groups, offsets.
  Recorded segments only.

## Rung 5: CDC from postgres logical decoding (kind `cdc`)

- Proven: change capture from a real postgres through the built-in
  `test_decoding` output plugin, consumed by polling SQL
  (`pg_create_logical_replication_slot`, `pg_logical_slot_get_changes`,
  `pg_drop_replication_slot`), in `ironwright/cdc.py` and
  `tests/test_cdc.py`. Insert, update and delete land with exact planted
  values as `_op`, `_table`, `_lsn` plus the changed columns, strings in
  bronze. Capture consumes: a second capture returns only new changes,
  and the mutation check (peek instead of get) fails that test. NULLs,
  quoted identifiers, and values with escaped quotes parse correctly. An
  update that changes the key lands the new tuple. A delete without a
  replica identity lands metadata only. A dropped slot errors loudly on
  the next capture. A line the parser does not understand raises with
  that line, never a silent drop. The connector spec
  `{kind: cdc, uri_env, slot, table}` runs end to end through
  `read_source`; the uri comes from the named env var, never from the
  spec, so no credentials live in pack data.
- Fixture: the shared `postgres_uri` and `pg_schema` fixtures against a
  real server named by `POSTGRES_URI` with `wal_level=logical`. Without
  it the live tests skip with the posture stated. The parser tests run
  offline against change lines captured verbatim from a real server.
- Not claimed: Debezium itself, MySQL, Oracle, or any non-postgres
  source, streaming protocol consumption (this polls), and exactly-once
  across restarts beyond what replication slot semantics give. Changes
  made before the slot exists are gone, by design.

## Dependencies

Runtime deps for rungs 1 and 2 live in the new `connectors` extra
(boto3, adbc-driver-manager, adbc-driver-sqlite). moto is dev-only.
Rungs 3 and 4 need nothing beyond the base install. Rung 5 needs
psycopg, which today lives in the `dev` extra; moving it to a runtime
extra is the integrator's call since `pyproject.toml` is theirs.

## Not shipped

SFTP (the other half of rung 1 in the sketch) is not started and is not
claimed anywhere.
