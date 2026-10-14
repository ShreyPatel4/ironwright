"""Domain packs: one mock company per industry, all running on the same engine.

A pack is a folder with:
  pack.yaml        story, legacy sources, quality checks, reconciliation
  generator.py     builds the messy legacy systems and the ground truth
  models/silver/*.sql, models/gold/*.sql   transforms (DuckDB SQL)
  ontology.yaml    object types, resolution, precedence, links, functions, actions
  policies.yaml    Cedar-style permit/forbid rules
  usecases.yaml    agent decisions, evidence, diagnosis rules
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from .config import PACKS_DIR, PackPaths


@dataclass
class Pack:
    name: str
    dir: Path
    spec: dict[str, Any]
    ontology: dict[str, Any]
    policies: dict[str, Any]
    usecases: dict[str, Any]
    paths: PackPaths = field(init=False)

    def __post_init__(self) -> None:
        self.paths = PackPaths(self.name)

    @property
    def title(self) -> str:
        return self.spec.get("title", self.name)

    def generator(self) -> ModuleType:
        spec = importlib.util.spec_from_file_location(f"ontoforge_pack_{self.name}", self.dir / "generator.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        return mod

    def models(self, layer: str) -> list[tuple[str, str]]:
        """(table_name, sql) for a layer, in file-name order (prefix files with 01_, 02_ to order)."""
        d = self.dir / "models" / layer
        out = []
        for f in sorted(d.glob("*.sql")):
            name = f.stem.split("_", 1)[1] if f.stem[:2].isdigit() else f.stem
            out.append((name, f.read_text()))
        return out


def _yaml(p: Path) -> dict[str, Any]:
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def load_pack(name: str) -> Pack:
    d = PACKS_DIR / name
    if not (d / "pack.yaml").exists():
        raise SystemExit(f"Unknown pack '{name}'. Available: {', '.join(list_packs())}")
    return Pack(name=name, dir=d, spec=_yaml(d / "pack.yaml"), ontology=_yaml(d / "ontology.yaml"),
                policies=_yaml(d / "policies.yaml"), usecases=_yaml(d / "usecases.yaml"))


def list_packs() -> list[str]:
    return sorted(p.name for p in PACKS_DIR.iterdir() if (p / "pack.yaml").exists())
