"""Org generator for the regional_retail_chain archetype.

This is the ROADMAP's 16-store regional chain, generalized: `size` is the
store count. The generator draws every choice from one random.Random passed
in explicitly, so the same spec and seed always produce the same org and the
global random state is never touched.

All names are invented. Company, region and town names come from fixed pools
of made-up words in the same style as the hand-written packs (Brightline,
Kestrel, Northfield). No real people, no real companies.
"""
from __future__ import annotations

import random

from .org import ApproverChain, Department, Org, Region, Role, Store
from .spec import ScenarioSpec

# ---------------------------------------------------------------- name pools

COMPANY_FIRST = [
    "Copperleaf", "Fernway", "Northgate", "Larkspur", "Sablewood", "Quillstone",
    "Harborlight", "Mossbrook", "Tindervale", "Gildenrow", "Bluestem", "Ridgemark",
]
COMPANY_SECOND = ["Market", "Mercantile", "Goods", "Trading", "Supply"]

REGION_POOL = [
    ("NR", "North Range"),
    ("LK", "Lakeshore"),
    ("HV", "High Valley"),
    ("CP", "Coastal Plain"),
    ("BW", "Birchwood"),
    ("SR", "South Reach"),
]

TOWN_FIRST = [
    "Fern", "Alder", "Bram", "Cinder", "Dove", "Elm", "Gran", "Haw",
    "Linden", "Mar", "Oak", "Pell", "Quin", "Ros", "Tam", "Wic",
]
TOWN_SECOND = ["brook", "crest", "dale", "field", "ford", "haven", "mill", "wick"]

CATEGORY_POOL = [
    ("skin_care", "Skin care"),
    ("hair_care", "Hair care"),
    ("pet_food", "Pet food"),
    ("pet_care", "Pet care"),
    ("wellness", "Wellness"),
    ("home_goods", "Home goods"),
]

# object type -> role id. The CDO owns Customer, per the roadmap's example.
DATA_OWNERS = {
    "customer": "cdo",
    "item": "cmo",
    "vendor": "cmo",
    "store": "coo",
    "planogram": "space_planning_lead",
    "sales_transaction": "controller",
    "inventory": "coo",
}


def generate(spec: ScenarioSpec, rng: random.Random) -> Org:
    """Expand a validated spec into the org layer. All randomness comes from rng."""
    # Draw order is part of the determinism contract: company, regions,
    # towns, region assignment, categories. Never reorder these draws.
    company = f"{rng.choice(COMPANY_FIRST)} {rng.choice(COMPANY_SECOND)}"
    company_short = company.split()[0]

    n_regions = rng.randint(2, min(4, spec.size))
    region_picks = rng.sample(REGION_POOL, n_regions)

    towns = rng.sample([a + b for a in TOWN_FIRST for b in TOWN_SECOND], spec.size)

    # every region gets at least one store, the rest fall where they fall
    assignment = [code for code, _ in region_picks]
    assignment += [rng.choice(region_picks)[0] for _ in range(spec.size - n_regions)]

    n_categories = rng.randint(3, 5)
    categories = rng.sample(CATEGORY_POOL, n_categories)

    # ------------------------------------------------------------ departments
    departments = (
        Department("executive", "Executive", "ceo"),
        Department("store_operations", "Store operations", "coo"),
        Department("merchandising", "Merchandising", "cmo"),
        Department("finance", "Finance", "cfo"),
        Department("data_governance", "Data governance", "cdo"),
    )

    # ------------------------------------------------------------------ roles
    plan: list[tuple[str, str, str, str | None]] = [
        ("ceo", "Chief Executive Officer", "executive", None),
        ("coo", "Chief Operating Officer", "store_operations", "ceo"),
        ("cmo", "Chief Merchandising Officer", "merchandising", "ceo"),
        ("cfo", "Chief Financial Officer", "finance", "ceo"),
        ("cdo", "Chief Data Officer", "data_governance", "ceo"),
        ("controller", "Controller", "finance", "cfo"),
        ("space_planning_lead", "Space Planning Lead", "merchandising", "cmo"),
        ("data_steward", "Data Steward", "data_governance", "cdo"),
    ]
    for cat_id, cat_name in categories:
        plan.append((f"category_manager_{cat_id}", f"Category Manager, {cat_name}",
                     "merchandising", "cmo"))
    for code, name in region_picks:
        plan.append((f"regional_manager_{code.lower()}", f"Regional Manager, {name}",
                     "store_operations", "coo"))

    stores = []
    for i, (town, code) in enumerate(zip(towns, assignment)):
        number = f"{101 + i:04d}"
        manager = f"store_manager_{number}"
        plan.append((manager, f"Store Manager, {company_short} #{number}",
                     "store_operations", f"regional_manager_{code.lower()}"))
        stores.append(Store(number=number, name=f"{company_short} #{number} {town}",
                            region=code, manager_role=manager))

    # ------------------------------------------------------- approver chains
    chains = [
        ApproverChain("propose_facing_change", ("space_planning_lead", "cmo")),
        ApproverChain("delist_item", ("cmo", "ceo")),
        ApproverChain("close_customer_record", ("data_steward", "cdo")),
        ApproverChain("onboard_vendor", ("cmo", "cfo")),
    ]
    for code, _ in region_picks:
        chains.append(ApproverChain(f"approve_store_order_{code.lower()}",
                                    (f"regional_manager_{code.lower()}", "coo")))

    approves: dict[str, list[str]] = {}
    for ch in chains:
        for role_id in ch.chain:
            acts = approves.setdefault(role_id, [])
            if ch.action not in acts:
                acts.append(ch.action)

    owns: dict[str, list[str]] = {}
    for obj in sorted(DATA_OWNERS):
        owns.setdefault(DATA_OWNERS[obj], []).append(obj)

    roles = tuple(
        Role(id=rid, title=title, department=dept, reports_to=boss,
             owns=tuple(owns.get(rid, ())), approves=tuple(approves.get(rid, ())))
        for rid, title, dept, boss in plan
    )

    regions = tuple(
        Region(code=code, name=name, manager_role=f"regional_manager_{code.lower()}")
        for code, name in region_picks
    )

    return Org(
        scenario=spec.name,
        archetype=spec.archetype,
        seed=spec.seed,
        company=company,
        departments=departments,
        roles=roles,
        regions=regions,
        stores=tuple(stores),
        data_owners=dict(DATA_OWNERS),
        approver_chains=tuple(chains),
    )
