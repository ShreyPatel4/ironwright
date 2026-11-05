"""Small fixes from docs/review/2026-11-product-review.md, one test per finding."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _demo_check_tomorrow(tz: str) -> str:
    code = ("import importlib.util as u, sys;"
            "s = u.spec_from_file_location('c', sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m);"
            "print(m.tomorrow_utc())")
    done = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts" / "demo_console_check.py")],
                          capture_output=True, text=True, env={**os.environ, "TZ": tz}, check=True)
    return done.stdout.strip()


@pytest.mark.parametrize("tz", ["Etc/GMT+12", "Etc/GMT-14"])  # 26 hours apart: one is always off UTC's date
def test_f14_demo_check_grant_is_tomorrow_in_utc_whatever_the_laptop_says(tz):
    """A grant dies at the start of its date in UTC. Local tomorrow can already be today there."""
    want = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    assert _demo_check_tomorrow(tz) == want
