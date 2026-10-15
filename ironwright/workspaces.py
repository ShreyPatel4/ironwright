"""Scenario workspaces: organizations a user creates from scratch.

A created organization is the scenario engine's output persisted under the
data root: scenario.yaml (the spec, which doubles as the L2 provenance
marker the verification module keys on) and org.json (the generated org).
Packs are code-shipped and immutable from here; organizations are user
data and can be deleted. The two never share a name.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from .config import data_root
from .packs import list_packs
from .scenario import ARCHETYPES, generate_org
from .scenario.spec import ScenarioSpec, SpecError

_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")


def _dir(name: str) -> Path:
    return data_root() / name


def list_organizations() -> list[dict[str, Any]]:
    root = data_root()
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if (d / "scenario.yaml").exists() and (d / "org.json").exists():
            org = json.loads((d / "org.json").read_text())
            out.append({"name": d.name, "company": org.get("company"),
                        "archetype": org.get("archetype"), "seed": org.get("seed"),
                        "stores": len(org.get("stores", [])),
                        "roles": len(org.get("roles", []))})
    return out


def read_org(name: str) -> dict[str, Any]:
    path = _dir(name) / "org.json"
    if not path.exists():
        raise FileNotFoundError(name)
    return json.loads(path.read_text())


def create_organization(name: str, archetype: str, size: int, seed: int) -> dict[str, Any]:
    if not _NAME.match(name):
        raise SpecError("name must be a slug: lowercase letter first, then "
                        "lowercase letters, digits, - or _, at most 40 chars")
    if name in list_packs():
        raise SpecError(f"{name} is a shipped pack; organizations cannot shadow packs")
    if (_dir(name) / "scenario.yaml").exists():
        raise SpecError(f"organization {name} already exists")
    if archetype not in ARCHETYPES:
        raise SpecError(f"unknown archetype {archetype}; have: {', '.join(sorted(ARCHETYPES))}")
    spec = ScenarioSpec(name=name, archetype=archetype, size=size, seed=seed)
    org = generate_org(spec)
    d = _dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "scenario.yaml").write_text(yaml.safe_dump(
        {"name": name, "archetype": archetype, "size": size, "seed": seed},
        sort_keys=True))
    (d / "org.json").write_text(json.dumps(org.to_dict(), indent=1, sort_keys=True))
    return org.to_dict()


def delete_organization(name: str) -> None:
    d = _dir(name)
    if not (d / "scenario.yaml").exists():
        # Refuse to delete anything that is not provably a created
        # organization; packs and foreign directories are untouchable here.
        raise FileNotFoundError(name)
    shutil.rmtree(d)


def archetypes() -> list[dict[str, Any]]:
    return [{"name": n, "size_means": "stores", "size_min": 4, "size_max": 64}
            for n in sorted(ARCHETYPES)]
