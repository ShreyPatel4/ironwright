"""Paths and settings. Everything lives under one data directory so a run is easy to wipe or ship."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
PACKS_DIR = PACKAGE_DIR / "packs"


def data_root() -> Path:
    return Path(os.environ.get("IRONWRIGHT_DATA", Path.cwd() / "data")).resolve()


@dataclass(frozen=True)
class PackPaths:
    """Where each layer of one pack's run is stored. Mirrors the four-layer stack."""

    pack: str

    @property
    def root(self) -> Path:
        return data_root() / self.pack

    @property
    def legacy(self) -> Path:          # RAW SOURCES: what the old systems emit
        return self.root / "legacy"

    @property
    def lake(self) -> Path:            # LOGIC layer storage: Iceberg medallion
        return self.root / "lake"

    @property
    def ontology_db(self) -> Path:     # ONTOLOGY layer: objects, links, actions
        return self.root / "ontology.duckdb"

    @property
    def checkpoints(self) -> Path:     # APPLICATION layer: agent thread state
        return self.root / "checkpoints.sqlite"

    @property
    def traces(self) -> Path:
        return self.root / "traces"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def ground_truth(self) -> Path:    # what the generator planted, used only by evals
        return self.legacy / "_ground_truth.json"

    def ensure(self) -> "PackPaths":
        for p in (self.legacy, self.lake, self.traces, self.reports):
            p.mkdir(parents=True, exist_ok=True)
        return self


def setting(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"IRONWRIGHT_{name}", default)
