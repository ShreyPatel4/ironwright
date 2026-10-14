"""Build the ontology from silver: resolve identity, apply precedence, create links.

Identity is the hardest problem in a migration: the same vendor, customer or
shipment arrives from several legacy systems with different keys and slightly
different values. Resolution here is transparent on purpose:
  1. exact match on normalized keys (tax id, GTIN, policy number...)
  2. fuzzy match on a name-like field inside blocks, above a threshold
  3. pairs in the grey zone go to a review queue instead of being merged
The ontology owns the ID; a crosswalk maps every source key to it, so source
systems can be retired without breaking anything above.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any

from ..lake import Lakehouse
from ..packs import Pack
from ..tracing import Tracer
from .store import OntologyStore

_SUFFIXES = r"\b(inc|incorporated|co|company|corp|corporation|llc|ltd|limited|plc|group|the)\b"


def normalize(value: Any, how: str | None) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if how == "digits":
        d = re.sub(r"\D", "", s).lstrip("0")
        return d or None
    if how == "name":
        s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
        s = re.sub(_SUFFIXES, " ", s)
        return re.sub(r"\s+", " ", s).strip() or None
    if how == "lower":
        return s.lower()
    if how == "alnum":
        return re.sub(r"[^a-z0-9]", "", s.lower()) or None
    return s


class _UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}
        self.how: dict[str, tuple[str, float]] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str, method: str, conf: float) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)
            self.how[min(ra, rb)] = (method, conf) if min(ra, rb) not in self.how else self.how[min(ra, rb)]


def _sim(a: str, b: str) -> float:
    s1 = SequenceMatcher(None, a, b).ratio()
    s2 = SequenceMatcher(None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))).ratio()
    return max(s1, s2)


def build_ontology(pack: Pack, tracer: Tracer) -> dict[str, Any]:
    ont = pack.ontology
    lake = Lakehouse(pack)
    store = OntologyStore(pack.paths.ontology_db)
    store.reset()
    con = lake.connect()
    report: dict[str, Any] = {"object_types": {}, "links": {}}
    xw: dict[tuple[str, str, str], str] = {}

    with tracer.span("ontology.build", pack=pack.name):
        for tname, tdef in ont["object_types"].items():
            with tracer.span("ontology.resolve", object_type=tname) as sp:
                res = tdef.get("resolution", {})
                norm = res.get("normalize", {})
                cands: dict[str, dict[str, Any]] = {}
                for src in tdef["sources"]:
                    view = src["table"].replace(".", "_")
                    cols = {f: c for f, c in src["fields"].items()}
                    sel = ", ".join([f"{src['key']} as __key"] + [f"{c} as \"{f}\"" for f, c in cols.items()])
                    where = f" where {src['where']}" if src.get("where") else ""
                    for r in con.execute(f"select {sel} from {view}{where}").fetchdf().to_dict("records"):
                        if r["__key"] is None:
                            continue
                        mid = f"{src['system']}:{r.pop('__key')}"
                        cands[mid] = {"system": src["system"], "fields": {k: (None if v != v else v) for k, v in r.items()}}
                uf = _UF()
                for m in cands:
                    uf.find(m)
                # 1. exact keys
                for f in res.get("exact", []):
                    seen: dict[str, str] = {}
                    for m, c in cands.items():
                        v = normalize(c["fields"].get(f), norm.get(f, "lower"))
                        if v is None:
                            continue
                        if v in seen:
                            uf.union(seen[v], m, f"exact:{f}", 1.0)
                        else:
                            seen[v] = m
                # 2. fuzzy, blocked by a prefix of the normalized field
                review = []
                fz = res.get("fuzzy")
                if fz:
                    f, thr, low = fz["field"], float(fz["threshold"]), float(fz.get("review_from", fz["threshold"]))
                    blocks: dict[str, list[tuple[str, str]]] = {}
                    for m, c in cands.items():
                        v = normalize(c["fields"].get(f), norm.get(f, "name"))
                        if v:
                            bk = v[:fz.get("block_prefix", 3)] + "|" + "|".join(
                                str(c["fields"].get(b)) for b in fz.get("block_on", []))
                            blocks.setdefault(bk, []).append((m, v))
                    for items in blocks.values():
                        for (m1, v1), (m2, v2) in combinations(items, 2):
                            if cands[m1]["system"] == cands[m2]["system"] and not fz.get("within_system", False):
                                continue
                            if uf.find(m1) == uf.find(m2):
                                continue
                            vetoed = False
                            for vf in fz.get("veto_if_conflict", []):
                                a = normalize(cands[m1]["fields"].get(vf), norm.get(vf, "lower"))
                                b = normalize(cands[m2]["fields"].get(vf), norm.get(vf, "lower"))
                                if a and b and a != b:
                                    vetoed = True
                            if fz.get("require_same_digits") and re.sub(r"\D", "", v1) != re.sub(r"\D", "", v2):
                                vetoed = True
                            if vetoed:
                                continue
                            s = _sim(v1, v2)
                            if s >= thr:
                                uf.union(m1, m2, f"fuzzy:{f}", round(s, 3))
                            elif s >= low:
                                review.append((m1, m2, round(s, 3)))
                # 3. clusters -> ontology objects
                clusters: dict[str, list[str]] = {}
                for m in cands:
                    clusters.setdefault(uf.find(m), []).append(m)
                prec = tdef.get("precedence", {})
                default_order = prec.get("default", [s["system"] for s in tdef["sources"]])
                prefix = tdef.get("prefix", tname[:3].upper())
                merged = 0
                obj_rows, hist_rows, xw_rows, merge_rows = [], [], [], []
                now = time.time()
                for root, members in clusters.items():
                    members.sort()
                    oid = f"{prefix}-{hashlib.sha1(members[0].encode()).hexdigest()[:8]}"
                    method, conf = uf.how.get(root, ("single", 1.0)) if len(members) > 1 else ("single", 1.0)
                    merged += len(members) > 1
                    props: dict[str, Any] = {}
                    prov: dict[str, str] = {}
                    fields = list(dict.fromkeys(f for m in members for f in cands[m]["fields"]))
                    for f in fields:
                        order = prec.get(f, default_order)
                        by_sys = {cands[m]["system"]: cands[m]["fields"].get(f) for m in members}
                        for sysname in order + [s for s in by_sys if s not in order]:
                            v = by_sys.get(sysname)
                            if v is not None:
                                props[f], prov[f] = v, sysname
                                break
                    props["_prov"] = prov
                    title = str(props.get(tdef.get("title", "name"), oid))
                    pj = json.dumps(props, default=str, sort_keys=True)
                    obj_rows.append([tname, oid, title, pj, 1, now])
                    hist_rows.append([tname, oid, 1, pj, "ontology.build", None, now])
                    for m in members:
                        sysname, key = m.split(":", 1)
                        xw_rows.append([tname, oid, sysname, key, method, conf])
                        xw[(tname, sysname, key)] = oid
                    merge_rows.append([tname, oid, json.dumps(members), method, conf, now])
                store.bulk("objects", obj_rows)
                store.bulk("object_history", hist_rows)
                store.bulk("crosswalk", xw_rows)
                store.bulk("merge_log", merge_rows)
                for m1, m2, s in review:
                    store.con.execute("insert into review_queue values (?, ?, ?, ?, ?, ?)",
                                      [tname, m1, m2, s, "fuzzy score in review band", time.time()])
                sp.set(candidates=len(cands), objects=len(clusters), merged=merged, review=len(review))
                report["object_types"][tname] = {"candidates": len(cands), "objects": len(clusters),
                                                 "merged_clusters": merged, "review_queue": len(review),
                                                 "owner": tdef.get("owner")}

        for lname, ldef in ont.get("link_types", {}).items():
            with tracer.span("ontology.link", link_type=lname) as sp:
                rows = con.execute(ldef["sql"]).fetchdf().to_dict("records")
                made, orphans = 0, 0
                seen = set()
                link_rows = []
                for r in rows:
                    a = xw.get((ldef["from"], str(r["from_system"]), str(r["from_key"])))
                    b = xw.get((ldef["to"], str(r["to_system"]), str(r["to_key"])))
                    if not a or not b:
                        orphans += 1
                        continue
                    if (a, b) in seen and not ldef.get("multi", False):
                        continue
                    seen.add((a, b))
                    props = {k: (None if v != v else v) for k, v in r.items()
                             if k not in ("from_system", "from_key", "to_system", "to_key")}
                    link_rows.append([lname, ldef["from"], a, ldef["to"], b, json.dumps(props, default=str)])
                    made += 1
                if link_rows:
                    store.bulk("links", link_rows)
                sp.set(links=made, orphans=orphans)
                report["links"][lname] = {"links": made, "orphans": orphans}
    report["stats"] = store.stats()
    store.con.close()
    return report
