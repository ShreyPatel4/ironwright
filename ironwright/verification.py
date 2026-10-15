"""Verification levels, derived from artifacts on disk. Never hand-set.

- L3: the generator's ground truth exists AND the evals report exists and is
  green. The four packs after a run.
- L2: a generated scenario (scenario.yaml provenance in the workspace root)
  whose packs carry structural invariants only. Zero instances today; the
  level exists so generated is never confused with proven.
- L1: everything else, including a pack that has not run on this machine and
  a user's own files. No ground truth; the badge says exactly that.

The UI renders this on every workspace and every answer; a test asserts the
derivation matches the artifacts, so overclaiming a level is a red suite.
"""
from __future__ import annotations

import json
from typing import Any

from .packs import Pack


def derive_organization(org_dir) -> dict[str, Any]:
    """A created organization is L2 by construction: generated, with
    structural invariants behind the generator's tests, and no ground truth
    until the scenario engine emits runnable packs."""
    return {"level": "L2", "ground_truth": False, "evals_green": False,
            "scenario_provenance": True,
            "meaning": "generated scenario, structural invariants only"}


def derive(pack: Pack) -> dict[str, Any]:
    ground_truth = pack.paths.ground_truth.exists()
    evals_path = pack.paths.reports / "evals.json"
    evals_green = False
    if evals_path.exists():
        try:
            evals_green = json.loads(evals_path.read_text()).get("passed") is True
        except (json.JSONDecodeError, OSError):
            evals_green = False
    scenario = (pack.paths.root / "scenario.yaml").exists()

    if ground_truth and evals_green:
        level = "L3"
    elif scenario:
        level = "L2"
    else:
        level = "L1"
    return {
        "level": level,
        "ground_truth": ground_truth,
        "evals_green": evals_green,
        "scenario_provenance": scenario,
        "meaning": {
            "L3": "planted ground truth, evals green on this machine",
            "L2": "generated scenario, structural invariants only",
            "L1": "no ground truth on this machine; structural checks only",
        }[level],
    }
