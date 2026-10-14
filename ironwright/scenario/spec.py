"""Scenario spec: one small YAML file that names a company to generate.

A spec is {name, archetype, size, seed} and nothing else. Validation fails
loud: unknown keys, missing keys, wrong types and out-of-range values all
raise SpecError with a message that names the offending key.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

MAX_SEED = 2**63 - 1  # fits in a signed 64-bit int everywhere we export
MIN_SIZE, MAX_SIZE = 4, 64
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _-]{0,63}$")

REQUIRED_KEYS = ("name", "archetype", "size", "seed")


class SpecError(ValueError):
    """A scenario spec is malformed. The message says which key and why."""


def _require_int(value, key: str, lo: int, hi: int) -> int:
    # bool is an int subclass; a spec saying `size: true` is a mistake, not a size
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{key} must be an integer, got {value!r}")
    if not lo <= value <= hi:
        raise SpecError(f"{key} must be between {lo} and {hi}, got {value}")
    return value


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    archetype: str
    size: int
    seed: int

    @classmethod
    def from_dict(cls, raw) -> "ScenarioSpec":
        from . import ARCHETYPES  # late import: the registry lives in __init__

        if not isinstance(raw, dict):
            raise SpecError(f"a scenario spec must be a mapping, got {type(raw).__name__}")

        unknown = sorted(set(raw) - set(REQUIRED_KEYS))
        if unknown:
            raise SpecError(f"unknown keys in scenario spec: {', '.join(unknown)}")
        missing = [k for k in REQUIRED_KEYS if k not in raw]
        if missing:
            raise SpecError(f"missing keys in scenario spec: {', '.join(missing)}")

        name = raw["name"]
        if not isinstance(name, str) or not NAME_RE.match(name.strip()) or not name.strip():
            raise SpecError(
                f"name must be 1 to 64 letters, digits, spaces, dashes or underscores, got {name!r}"
            )

        archetype = raw["archetype"]
        if archetype not in ARCHETYPES:
            known = ", ".join(sorted(ARCHETYPES))
            raise SpecError(f"unknown archetype {archetype!r}, known archetypes: {known}")

        size = _require_int(raw["size"], "size", MIN_SIZE, MAX_SIZE)
        seed = _require_int(raw["seed"], "seed", 0, MAX_SEED)

        return cls(name=name.strip(), archetype=archetype, size=size, seed=seed)


def load_spec(path: str | Path) -> ScenarioSpec:
    """Read one scenario YAML file and validate it."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"could not parse {path}: {exc}") from exc
    return ScenarioSpec.from_dict(raw)
