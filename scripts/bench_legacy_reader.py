"""Benchmark the Python legacy reader. The number that gates any Rust.

Rule from docs/platform-architecture.md: no extraction into a systems
language without first publishing the Python number it must beat by 10x
on the same corpus. This script is that first half: a deterministic
seeded fixed-width corpus (committed generator, reproducible by sha256),
read through the real read_source() path, p50 of 5 runs, machine stated.

Usage: python scripts/bench_legacy_reader.py [--mb 100] [--seed 7]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ironwright.legacy import read_source  # noqa: E402

LAYOUT = [["item_id", 12], ["store_id", 6], ["vendor_id", 8], ["description", 40],
          ["qty", 8], ["unit_cost", 10], ["status", 4], ["updated", 8]]
WIDTH = sum(w for _, w in LAYOUT)


def build_corpus(path: Path, target_mb: int, seed: int) -> tuple[int, str]:
    rng = random.Random(seed)
    words = ["ANVIL", "BOLT", "CLEAT", "DOWEL", "EAVE", "FLUX", "GUSSET", "HASP"]
    rows = 0
    h = hashlib.sha256()
    with path.open("w", encoding="latin-1") as f:
        while path.exists() and f.tell() < target_mb * 1024 * 1024:
            rec = (f"{rng.randrange(10**11):012d}" f"{rng.randrange(999999):06d}"
                   f"V-{rng.randrange(10**5):05d} "
                   f"{' '.join(rng.choices(words, k=3)):<40.40}"
                   f"{rng.randrange(10**7):08d}" f"{rng.uniform(0, 9999):010.4f}"
                   f"{rng.choice(['A', 'D', 'H', 'R']):<4}" f"2026{rng.randrange(1, 13):02d}{rng.randrange(1, 29):02d}")
            assert len(rec) == WIDTH, len(rec)
            f.write(rec + "\n")
            h.update(rec.encode("latin-1"))
            rows += 1
    return rows, h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=100)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        legacy_dir = Path(td)
        corpus = legacy_dir / "bench.fw"
        rows, digest = build_corpus(corpus, args.mb, args.seed)
        size_mb = corpus.stat().st_size / 1024 / 1024
        src = {"kind": "fixed_width", "file": "bench.fw", "layout": LAYOUT}

        # one warm-up pass so the page cache state is the same for all runs
        read_source(src, legacy_dir)
        times = []
        for _ in range(args.runs):
            t0 = time.perf_counter()
            table = read_source(src, legacy_dir)
            times.append(time.perf_counter() - t0)
        p50 = statistics.median(times)

        chip = ""
        if platform.system() == "Darwin":
            chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                  capture_output=True, text=True).stdout.strip()
        report = {
            "corpus": {"rows": rows, "mb": round(size_mb, 1), "seed": args.seed,
                       "record_width": WIDTH, "sha256": digest},
            "runs": args.runs, "warm_cache": True,
            "p50_seconds": round(p50, 3),
            "mb_per_second": round(size_mb / p50, 1),
            "rows_per_second": int(rows / p50),
            "arrow_columns": table.num_columns,
            "python": platform.python_version(), "machine": chip or platform.machine(),
        }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
