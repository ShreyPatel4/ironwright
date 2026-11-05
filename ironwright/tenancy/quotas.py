"""Plans and the limits they set. One tenant cannot burn the stack or the LLM budget.

Plans are data. An org with no row in org_plans is on free. No tenant route can
change a plan: billing is a non-goal, so an owner who could pick `team` would make
every limit here decorative. The operator calls set_plan.

The dev org has no limits. It is what IRONWRIGHT_DEV_OPEN maps to, and it runs the
demo packs, which no tenant can write to. A tenant may Ask a demo pack, and that
question spends the tenant's own limit.
"""
from __future__ import annotations

import contextlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException

from ..config import uploads_root
from ..llm import LLM, resolve_llm
from . import byokey, db, service, usage
from .models import DEV_ORG_ID

log = logging.getLogger("ironwright.quotas")
GIB = 1024 ** 3
DEFAULT_PLAN = "free"
PLANS: dict[str, dict[str, int]] = {
    "free": {"storage_bytes": GIB, "uploads": 20, "runs": 20, "ask": 100,
             "llm_tokens": 50_000, "members": 5},
    "team": {"storage_bytes": 50 * GIB, "uploads": 200, "runs": 200, "ask": 2_000,
             "llm_tokens": 1_000_000, "members": 50},
}
_DAILY_WORDS = {"uploads": "uploads", "runs": "runs", "ask": "Ask questions", "llm_tokens": "model tokens"}


class QuotaExceeded(HTTPException):
    def __init__(self, detail: str, retry_after: int | None = None) -> None:
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        super().__init__(429, detail, headers=headers)


def plan_of(org_id: str) -> str:
    with usage.engine().connect() as con:
        return con.execute(sa.select(usage.org_plans.c.plan)
                           .where(usage.org_plans.c.org_id == org_id)).scalar() or DEFAULT_PLAN


def limit(org_id: str, metric: str) -> int | None:
    """None means no limit."""
    return None if org_id == DEV_ORG_ID else PLANS[plan_of(org_id)][metric]


def set_plan(org_id: str, plan: str, actor: str, now: float) -> None:
    if plan not in PLANS:
        raise ValueError(f"no plan named {plan}")
    current = plan_of(org_id)
    if plan == current:
        return
    with usage.engine().begin() as con:
        con.execute(usage.org_plans.delete().where(usage.org_plans.c.org_id == org_id))
        con.execute(usage.org_plans.insert().values(org_id=org_id, plan=plan))
        usage.audit(con, org_id, actor, "plan.change", {"from": current, "to": plan}, now)


def seconds_to_midnight(now: float) -> int:
    today = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((today + timedelta(days=1)).timestamp() - now))


def _over_today(org_id: str, metric: str, cap: int, now: float) -> QuotaExceeded:
    return QuotaExceeded(f"Your org has used its {cap} {_DAILY_WORDS[metric]} for today on the "
                         f"{plan_of(org_id)} plan. The count starts again at 00:00 UTC.",
                         retry_after=seconds_to_midnight(now))


def spend(org_id: str, metric: str, now: float) -> None:
    """Take one of a daily limit, or refuse with a 429 and count nothing."""
    cap = limit(org_id, metric)
    if cap is not None and not usage.charge(org_id, metric, cap, now):
        raise _over_today(org_id, metric, cap, now)


def check(org_id: str, metric: str, now: float) -> None:
    """Refuse if a daily limit is used up, and take nothing. For a door that has a
    write to make before it can spend, so a refusal lands before the write."""
    cap = limit(org_id, metric)
    if cap is not None and usage.used(org_id, metric, now) >= cap:
        raise _over_today(org_id, metric, cap, now)


def storage_used(org_id: str) -> int:
    """Bytes under the org's uploads, measured, so the number cannot drift from the disk.
    A lake on object storage is not in here: it is derived from these files."""
    total = 0
    for folder, _, names in os.walk(uploads_root(org_id)):
        for name in names:
            with contextlib.suppress(OSError):  # a folder being renamed or removed right now
                total += os.lstat(os.path.join(folder, name)).st_size
    return total


def _size(n: int) -> str:
    for unit, scale in (("GB", GIB), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} bytes"


def start_upload(org_id: str, content_length: str | None, now: float) -> None:
    """Storage first, so a refusal there does not spend one of today's uploads.

    A chunked body has no length and counts as 0 here. It can pass the limit by one
    request, which T3's caps bound. Then the org is full and the next one stops.
    """
    cap = limit(org_id, "storage_bytes")
    if cap is not None:
        held = storage_used(org_id)
        incoming = max(int(content_length), 0) if (content_length or "").isdecimal() else 0
        if held >= cap or held + incoming > cap:
            raise QuotaExceeded(f"This upload would take your org over its {_size(cap)} of storage on the "
                                f"{plan_of(org_id)} plan. {_size(held)} are in use.")
    spend(org_id, "uploads", now)


def refund_upload(org_id: str, now: float) -> None:
    """start_upload spent one and the upload was refused. No limit means nothing was spent."""
    if limit(org_id, "uploads") is not None:
        usage.refund(org_id, "uploads", now)


def _seats(org_id: str, now: float) -> set[str]:
    """Everyone with a seat: members, and whoever a live invite is holding one for."""
    with usage.engine().connect() as con:
        members = con.execute(sa.select(db.users.c.email)
                              .join(db.memberships, db.memberships.c.user_id == db.users.c.id)
                              .where(db.memberships.c.org_id == org_id)).scalars().all()
        invited = con.execute(sa.select(db.invites.c.email)
                              .where(db.invites.c.org_id == org_id, db.invites.c.expires_at > now)).scalars().all()
    return {*members, *invited}


def org_id_of(slug: str) -> str | None:
    with usage.engine().connect() as con:
        return con.execute(sa.select(db.orgs.c.id).where(db.orgs.c.slug == slug)).scalar()


def check_seat(slug: str, email: str, now: float) -> None:
    """Before an invite. Someone already holding a seat needs no new one."""
    org_id = org_id_of(slug)
    cap = limit(org_id, "members") if org_id else None
    if cap is None:
        return
    try:
        email = service.clean_email(email)
    except service.Invalid:
        return  # the invite itself refuses this, in its own words
    seats = _seats(org_id, now)
    if email not in seats and len(seats) >= cap:
        raise QuotaExceeded(f"Your org has all {cap} member seats in use on the {plan_of(org_id)} plan, "
                            "counting open invites.")


def report(org_id: str, now: float) -> dict[str, Any]:
    """What the console shows. No limit reads as null."""
    used = {m: usage.used(org_id, m, now) for m in _DAILY_WORDS}
    used["storage_bytes"] = storage_used(org_id)
    used["members"] = len(_seats(org_id, now))
    return {"plan": "dev" if org_id == DEV_ORG_ID else plan_of(org_id), "day": usage.day_of(now),
            "resets_in_seconds": seconds_to_midnight(now),
            "byo_llm_tokens": usage.used(org_id, "llm_tokens_byo", now),
            "metrics": {m: {"used": n, "limit": limit(org_id, m)} for m, n in used.items()}}


def _model(org_id: str, now: float) -> Any:
    """The model this org may use: its own key, then the platform's, then the template.

    A key that cannot be opened gets the template. It never falls through to the
    platform: the org chose its own provider, and its cells stay away from ours.
    None means the dev org with no key, where the caller keeps the choice it had.
    """
    try:
        org_key = byokey.load(org_id)
    except byokey.Unreadable:
        log.warning("org %s has an llm key this server cannot open", org_id)
        return LLM("offline")
    if org_key:
        return usage.MeteredLLM(resolve_llm(org_key), org_id, "llm_tokens_byo", now)
    if org_id == DEV_ORG_ID:
        return None
    llm = resolve_llm(None)
    return llm if llm.offline else usage.MeteredLLM(llm, org_id, "llm_tokens", now)


def _out_of_tokens(org_id: str, llm: Any, now: float) -> bool:
    """Only the platform's tokens have a limit. An org's own key is its own bill."""
    return (isinstance(llm, usage.MeteredLLM) and llm.metric == "llm_tokens"
            and usage.used(org_id, "llm_tokens", now) >= limit(org_id, "llm_tokens"))


def start_ask(org_id: str, now: float) -> Any:
    """The Ask door: the model this question may use, or a 429.

    Tokens are known only after a call, so a call can pass the limit and the next
    question is the one refused.
    """
    llm = _model(org_id, now)
    if _out_of_tokens(org_id, llm, now):
        cap = limit(org_id, "llm_tokens")
        raise QuotaExceeded(f"Your org has used its {cap} model tokens for today on the {plan_of(org_id)} plan. "
                            "The count starts again at 00:00 UTC, or an admin can add your own key.",
                            retry_after=seconds_to_midnight(now))
    spend(org_id, "ask", now)
    return llm


def narration_llm(org_id: str, now: float) -> Any:
    """For a job that narrates what rules already decided. A worker has no request to
    answer 429 to, and the rules' work is not what costs tokens, so over the limit
    the job still runs and the paragraph is left out."""
    llm = _model(org_id, now)
    return LLM("offline") if _out_of_tokens(org_id, llm, now) else llm
