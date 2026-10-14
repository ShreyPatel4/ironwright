"""Export the audit chain with per-row preimages, for client-side verification.

The site demo verifies the chain in the browser: sha256(preimage) must equal
hash, and each preimage must start with the previous row's hash. Shipping the
preimage (prev_hash + the engine's canonical serialization of the row) lets a
browser do real cryptographic verification without having to replicate
Python's json.dumps byte for byte.

Run `ironwright run --pack <pack>` first so the store exists.
Writes web/data/<pack>-audit-chain.json.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ironwright.config import data_root  # noqa: E402
from ironwright.ontology.store import OntologyStore, _j  # noqa: E402

pack = sys.argv[1] if len(sys.argv) > 1 else "retail"
root = pathlib.Path(__file__).resolve().parents[1]
store = OntologyStore(data_root() / pack / "ontology.duckdb")

cols = ", ".join(OntologyStore.AUDIT_COLS)
rows = []
prev = "genesis"
ok = True
for row in store.con.execute(
    f"select {cols}, prev_hash, hash from audit_log order by seq"
).fetchall():
    vals, p, h = list(row[:-2]), row[-2], row[-1]
    preimage = p + _j(vals)
    import hashlib

    if p != prev or hashlib.sha256(preimage.encode()).hexdigest() != h:
        ok = False
    rec = dict(zip(OntologyStore.AUDIT_COLS, vals))
    rows.append(
        {
            "seq": rec["seq"],
            "actor": rec["actor"],
            "on_behalf_of": rec["on_behalf_of"],
            "action": rec["action"],
            "status": rec["status"],
            "approved_by": rec["approved_by"],
            "prev_hash": p,
            "hash": h,
            "preimage": preimage,
        }
    )
    prev = h
store.con.close()

if not ok:
    raise SystemExit("chain does not verify server side; refusing to export")

out = root / "web" / "data" / f"{pack}-audit-chain.json"
out.write_text(json.dumps({"pack": pack, "rows": rows}, indent=0))
print(f"{out}: {len(rows)} rows, chain verified before export")
