"""Scenario workspaces: organizations a user creates from scratch.

A created organization is the scenario engine's output persisted under the
data root: scenario.yaml (the spec, which doubles as the L2 provenance
marker the verification module keys on) and org.json (the generated org).
Packs are code-shipped and immutable from here; organizations are user
data and can be deleted. The two never share a name.

Delete keys on a separate marker, written only here. scenario.yaml says what a
folder claims to be; the marker says Ironwright made this folder under this
name, so a foreign folder holding a scenario.yaml is never removed.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from .config import (
    UnsafePath,
    data_root,
    private_dir,
    safe_child,
    secure_data_root,
    write_private,
)
from .packs import list_packs
from .scenario import ARCHETYPES, generate_org
from .scenario.spec import ScenarioSpec, SpecError

_NAME = re.compile(r"[a-z][a-z0-9_-]{1,39}")
_MARKER = ".ironwright-workspace"
# Folders the platform keeps directly under the data root. An organization
# with one of these names would own them, and deleting it would delete them.
_RESERVED = {"auth"}


def _dir(name: str) -> Path:
    try:
        return safe_child(data_root(), name)
    except UnsafePath as e:
        raise FileNotFoundError(name) from e


def _org_file(name: str) -> Path:
    try:
        return safe_child(_dir(name), "org.json")
    except UnsafePath as e:
        raise FileNotFoundError(name) from e


def _created_here(d: Path, name: str) -> bool:
    marker = d / _MARKER
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        return json.loads(marker.read_text()).get("name") == name
    except (json.JSONDecodeError, OSError, AttributeError):
        return False


def list_organizations() -> list[dict[str, Any]]:
    root = data_root()
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if d.is_symlink() or (d / "org.json").is_symlink():
            continue
        if (d / "scenario.yaml").exists() and (d / "org.json").exists():
            org = json.loads((d / "org.json").read_text())
            out.append({"name": d.name, "company": org.get("company"),
                        "archetype": org.get("archetype"), "seed": org.get("seed"),
                        "stores": len(org.get("stores", [])),
                        "roles": len(org.get("roles", []))})
    return out


def read_org(name: str) -> dict[str, Any]:
    path = _org_file(name)
    if not path.exists():
        raise FileNotFoundError(name)
    return json.loads(path.read_text())


def create_organization(name: str, archetype: str, size: int, seed: int) -> dict[str, Any]:
    if not _NAME.fullmatch(name):
        raise SpecError("name must be a slug: lowercase letter first, then "
                        "lowercase letters, digits, - or _, at most 40 chars")
    if name in list_packs():
        raise SpecError(f"{name} is a shipped pack; organizations cannot shadow packs")
    if name in _RESERVED:
        raise SpecError(f"{name} is reserved by the platform")
    try:
        d = _dir(name)
    except FileNotFoundError as e:
        raise SpecError(f"{name} is a symlink under the data root, refused") from e
    if d.exists():
        # Adopting an existing folder would make its contents deletable.
        raise SpecError(f"{name} already exists under the data root")
    if archetype not in ARCHETYPES:
        raise SpecError(f"unknown archetype {archetype}; have: {', '.join(sorted(ARCHETYPES))}")
    spec = ScenarioSpec(name=name, archetype=archetype, size=size, seed=seed)
    org = generate_org(spec)
    secure_data_root()
    private_dir(d)
    write_private(d / _MARKER, json.dumps({"created_by": "ironwright", "name": name}))
    write_private(d / "scenario.yaml", yaml.safe_dump(
        {"name": name, "archetype": archetype, "size": size, "seed": seed},
        sort_keys=True))
    write_private(d / "org.json", json.dumps(org.to_dict(), indent=1, sort_keys=True))
    return org.to_dict()


def delete_organization(name: str) -> None:
    d = _dir(name)
    if not _created_here(d, name):
        # Refuse to delete anything that is not provably a created
        # organization; packs and foreign directories are untouchable here.
        raise FileNotFoundError(name)
    shutil.rmtree(d)


def archetypes() -> list[dict[str, Any]]:
    return [{"name": n, "size_means": "stores", "size_min": 4, "size_max": 64}
            for n in sorted(ARCHETYPES)]
