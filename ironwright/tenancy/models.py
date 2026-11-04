"""The request context, roles, and the dev org. No I/O in here."""
from __future__ import annotations

from dataclasses import dataclass

# Weakest first. The index is the rank.
ROLES = ("viewer", "member", "admin", "owner")

# IRONWRIGHT_DEV_OPEN and accounts seeded with `ironwright auth-set` live here.
# A signup can never land in it: the id is not a uuid4 and the slug is reserved.
DEV_ORG_ID = "0" * 32
DEV_ORG_SLUG = "dev"
DEV_USER_ID = "0" * 32


@dataclass(frozen=True)
class Ctx:
    org_id: str
    org_slug: str
    user_id: str
    role: str
    principal: str


def principal_of(user_id: str) -> str:
    return f"user:{user_id}"


def at_least(role: str, needed: str) -> bool:
    return ROLES.index(role) >= ROLES.index(needed)
