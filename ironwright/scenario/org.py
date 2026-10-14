"""The ORG layer of a generated company, as plain dataclasses.

Everything here is data: roles in a hierarchy, departments with owners,
stores, regions, who owns which object type and who approves which action.
to_dict() returns plain JSON data so json.dumps(..., sort_keys=True) on the
same org is byte-identical run to run.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Role:
    id: str
    title: str
    department: str
    reports_to: str | None  # role id; None only for the CEO
    owns: tuple[str, ...] = ()      # object types this role owns
    approves: tuple[str, ...] = ()  # actions this role appears in a chain for

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "department": self.department,
            "reports_to": self.reports_to,
            "owns": list(self.owns),
            "approves": list(self.approves),
        }


@dataclass(frozen=True)
class Department:
    id: str
    name: str
    owner_role: str  # role id accountable for the department

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "owner_role": self.owner_role}


@dataclass(frozen=True)
class Region:
    code: str
    name: str
    manager_role: str  # role id of the regional manager

    def to_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "manager_role": self.manager_role}


@dataclass(frozen=True)
class Store:
    number: str
    name: str
    region: str  # region code
    manager_role: str  # role id of the store manager

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "name": self.name,
            "region": self.region,
            "manager_role": self.manager_role,
        }


@dataclass(frozen=True)
class ApproverChain:
    action: str
    chain: tuple[str, ...]  # role ids in approval order; the last is terminal

    def to_dict(self) -> dict:
        return {"action": self.action, "chain": list(self.chain)}


@dataclass(frozen=True)
class Org:
    scenario: str
    archetype: str
    seed: int
    company: str
    departments: tuple[Department, ...] = ()
    roles: tuple[Role, ...] = ()
    regions: tuple[Region, ...] = ()
    stores: tuple[Store, ...] = ()
    data_owners: dict = field(default_factory=dict)  # object type -> role id
    approver_chains: tuple[ApproverChain, ...] = ()

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "archetype": self.archetype,
            "seed": self.seed,
            "company": self.company,
            "departments": [d.to_dict() for d in self.departments],
            "roles": [r.to_dict() for r in self.roles],
            "regions": [r.to_dict() for r in self.regions],
            "stores": [s.to_dict() for s in self.stores],
            "data_owners": dict(sorted(self.data_owners.items())),
            "approver_chains": [c.to_dict() for c in self.approver_chains],
        }
