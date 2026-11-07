"""The bridge routes. The app-wide gate scopes every /workspaces/{name} path to the caller's
org and checks the role, so past it an org may learn its own state: these answers read
differently from the gate's NOT_FOUND, which the isolation sweep holds apart.

PUT /gold and POST /gold/draft are admin (platform_api.WRITE_ROLES). A draft is never
saved: a human reads it and PUTs it. The model may draft and narrate. Coverage comes from
coverage.plan alone.
"""
from __future__ import annotations

import re
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import auth
from .. import workspaces_tenant as wt
from ..llm import LLM, boundary_rule, fence_record, new_nonce, untrusted
from ..tenancy import Ctx, quotas
from . import coverage, gold, planner

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(auth.ctx)]
NOT_TENANT = "this is not one of your workspaces, so it has no gold contract"
NO_GOLD = "no gold contract yet: write one, or draft one from your use cases"
MAX_USE_CASES = 10
_REPLY_CAP = 64 * 1024
_FENCED_YAML = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL)


class GoldBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    yaml: str | None = Field(default=None, max_length=gold.MAX_BYTES)
    contract: dict[str, Any] | None = None


class DraftBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_cases: list[Annotated[str, Field(min_length=1, max_length=600)]] = Field(min_length=1, max_length=MAX_USE_CASES)


def _folder(org_id: str, name: str):
    try:
        return wt._existing(org_id, name)
    except FileNotFoundError as e:
        raise HTTPException(404, NOT_TENANT) from e


def _saved(org_id: str, name: str) -> dict[str, Any]:
    path = _folder(org_id, name) / gold.FILE
    try:
        text = wt._read_small(path, gold.FILE)
    except FileNotFoundError as e:
        raise HTTPException(404, NO_GOLD) from e
    except wt.WorkspaceError as e:
        raise HTTPException(422, str(e)) from e
    try:
        return gold.parse(text)
    except gold.GoldError as e:  # the file was valid when saved, so this is a schema change
        raise HTTPException(422, f"the saved gold.yaml no longer validates: {e}") from e


def _sources(org_id: str, name: str) -> dict[str, Any]:
    try:
        return planner.workspace_sources(org_id, name)
    except FileNotFoundError as e:
        raise HTTPException(404, NOT_TENANT) from e
    except wt.WorkspaceError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/workspaces/{name}/gold")
def get_gold(name: str, c: CtxDep) -> dict[str, Any]:
    contract = _saved(c.org_id, name)
    return {"contract": contract, "yaml": gold.dump(contract)}


@router.put("/workspaces/{name}/gold")
def put_gold(name: str, body: GoldBody, c: CtxDep) -> dict[str, Any]:
    folder = _folder(c.org_id, name)
    if (body.yaml is None) == (body.contract is None):
        raise HTTPException(422, "send the contract as yaml text or as a contract object, not both")
    try:
        contract = gold.parse(body.yaml) if body.yaml is not None else gold.validate(body.contract)
    except gold.GoldError as e:
        raise HTTPException(422, str(e)) from e
    wt._replace(folder / gold.FILE, gold.dump(contract))
    return {"contract": contract, "yaml": gold.dump(contract)}


@router.get("/workspaces/{name}/bridge/plan")
def get_plan(name: str, c: CtxDep, narrate: bool = False) -> dict[str, Any]:
    contract = _saved(c.org_id, name)
    report = coverage.plan(contract, _sources(c.org_id, name))
    if narrate:
        report = coverage.narrate(report, _model(c))
    return report


# ------------------------------------------------------------------ drafting

def _model(c: Ctx) -> Any:
    return quotas.narration_llm(c.org_id, auth._now()) or LLM()


def _use_case_id(sentence: str, taken: set[str]) -> str:
    words = re.findall(r"[a-z0-9]+", sentence.lower())[:6]
    base = "-".join(words) or "use-case"
    base = base if base[0].isalpha() else f"u-{base}"
    out, n = base[:60], 2
    while out in taken:
        out, n = f"{base[:56]}-{n}", n + 1
    taken.add(out)
    return out


def _skeleton(sentences: list[str]) -> dict[str, Any]:
    taken: set[str] = set()
    return {"kind": "gold-contract", "version": 1,
            "use_cases": [{"id": _use_case_id(s, taken), "audience": "not named yet", "question": " ".join(s.split()),
                           "freshness": "not set", "consumer": {"type": "bi", "target": "not set"}} for s in sentences],
            "dimensions": [], "facts": [], "marts": []}


_SCHEMA_HINT = (
    "Write one YAML document and nothing else. Its shape:\n"
    "kind: gold-contract\nversion: 1\n"
    "use_cases: [{id: lowercase-dashed, audience: text, question: text, freshness: text, "
    "consumer: {type: bi|file|api|reverse_etl, target: text}}]\n"
    "dimensions: [{name: dim_x, grain: text, columns: [{name: snake_case, type: string|int|double|bool|date|"
    "timestamp|decimal(p,s), key: true}]}]   each dimension has exactly one key column\n"
    "facts: [{name: fct_x, grain: text, columns: [...same...]}]   a fact carries the key of each dimension it joins\n"
    "marts: [{name: mart_x, from: [fct_x, dim_x], use_case: an id above, rules: [\"col > col\"], "
    "access: {roles: [member], masking: {col: hash|mask|drop}}}]\n"
    "No other fields. Say nothing about which columns exist in the sources: that is decided elsewhere.")


def _reply_yaml(text: str) -> str:
    text = text[:_REPLY_CAP]
    m = _FENCED_YAML.search(text)
    return m.group(1) if m else text


def _draft_with(llm: Any, sentences: list[str], sources: dict[str, Any]) -> tuple[str | None, str]:
    nonce = new_nonce()
    asks = [untrusted(s, "use_case", nonce, cap=600) for s in sentences]
    cols = [fence_record({"table": t["name"], "column": col["name"], "type": col["type"]}, "source_column", nonce)
            for t in sources["tables"] for col in t["columns"]][:200]
    res = llm.complete("You draft a gold layer contract for a data team to review.\n" + boundary_rule(nonce),
                       "The business asks these questions:\n" + "\n".join(asks)
                       + "\nThe uploaded sources hold these columns:\n" + ("\n".join(cols) or "(none yet)")
                       + "\n" + _SCHEMA_HINT, max_tokens=2000)
    return res.text, res.model


@router.post("/workspaces/{name}/gold/draft")
def draft_gold(name: str, body: DraftBody, c: CtxDep) -> dict[str, Any]:
    """A proposal for a human to read, edit and PUT. Validated by the same rules as a PUT."""
    sources = _sources(c.org_id, name)
    llm = _model(c)
    if llm.offline:
        draft = _skeleton(body.use_cases)
        note = "No model is set up for this org, so I filled in the use cases. Add the tables and marts yourself."
        model = None
    else:
        text, model = _draft_with(llm, body.use_cases, sources)
        note = "A model drafted this from your use cases and source column names. Read it before you save it."
        try:
            draft = yaml.safe_load(_reply_yaml(text)) if text else None
        except yaml.YAMLError:
            draft = None
        if not isinstance(draft, dict):
            draft = _skeleton(body.use_cases)
            note = "The model's reply was not a YAML contract, so I filled in the use cases only."
    try:
        clean = gold.validate(draft)
        return {"draft": clean, "yaml": gold.dump(clean), "valid": True, "errors": [], "model": model, "note": note}
    except gold.GoldError as e:
        return {"draft": draft, "yaml": yaml.safe_dump(draft, sort_keys=False), "valid": False, "errors": [str(e)],
                "model": model, "note": note}
