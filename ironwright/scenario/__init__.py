"""Scenario engine, slice 1: spec in, org layer out.

A scenario is one small YAML file plus a seed. This slice expands it into the
ORG layer only (hierarchy, departments, data owners, approver chains) for one
archetype. Systems estate, problem catalog, governance generation and
migration waves are later slices (docs/ROADMAP.md, Phase 1).

    from ironwright.scenario import load_spec, generate_org
    org = generate_org(load_spec("scenario.yaml"))
    org.to_dict()  # plain JSON data, byte-stable under sort_keys=True
"""
from __future__ import annotations

import random

from . import regional_retail
from .org import ApproverChain, Department, Org, Region, Role, Store
from .spec import ScenarioSpec, SpecError, load_spec

# archetype name -> generator(spec, rng) -> Org
ARCHETYPES = {
    "regional_retail_chain": regional_retail.generate,
}


def generate_org(spec: ScenarioSpec) -> Org:
    """Expand a validated spec into its org. Deterministic in (spec, seed)."""
    rng = random.Random(spec.seed)
    return ARCHETYPES[spec.archetype](spec, rng)


__all__ = [
    "ARCHETYPES",
    "ApproverChain",
    "Department",
    "Org",
    "Region",
    "Role",
    "ScenarioSpec",
    "SpecError",
    "Store",
    "generate_org",
    "load_spec",
]
