"""The one road from a workspace name to the Pack a request reads.

The app gate has already said this org may see the name. This answers which thing the
name means for this org: a demo pack first, as at the gate, then a workspace in this
org's own folder. A slug is only unique inside an org, so nothing past this point may
look a tenant workspace up by its bare name.
"""
from __future__ import annotations

from fastapi import HTTPException

from . import workspaces_tenant as wt
from .ontology.propose import askable
from .packs import Pack, list_packs, load_pack
from .tenancy.models import Ctx


def find(name: str, c: Ctx) -> Pack | None:
    """The pack, with the reads its accepted ontology generates, or None if it is neither.
    A workspace whose spec no longer validates says so rather than reading as missing."""
    if name in list_packs():
        return load_pack(name)
    try:
        return askable(wt.load_workspace(c.org_id, name))
    except FileNotFoundError:
        return None
    except wt.WorkspaceError as e:
        raise HTTPException(422, str(e)) from e


def workspace_pack(name: str, c: Ctx) -> Pack:
    pack = find(name, c)
    if pack is None:  # past the gate, so this org may know
        raise HTTPException(404, f"no workspace named {name}")
    return pack


def is_tenant(name: str, c: Ctx) -> bool:
    """Whether the name is this org's own workspace. It asks without validating, so a
    workspace with a broken spec still counts as one."""
    if name in list_packs():
        return False
    try:
        wt.read_workspace(c.org_id, name)
    except FileNotFoundError:
        return False
    except wt.WorkspaceError:
        return True
    return True


def refuse_outbound(name: str, c: Ctx, where: str) -> None:
    """Tenant bytes never leave their org's storage prefix (docs/saas-mvp.md), so a route
    that copies a workspace somewhere else stops here for a tenant workspace."""
    if is_tenant(name, c):
        raise HTTPException(403, f"your workspace's data stays in your org's storage, so I do not send it to {where}")
