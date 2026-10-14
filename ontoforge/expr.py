"""A tiny safe expression evaluator.

Packs write rules, preconditions and policy conditions as short Python-like
expressions ("evidence.oos_days >= 3 and object.status == 'ACTIVE'").
Only comparisons, boolean logic, arithmetic, literals, lookups and a few pure
functions are allowed, so a pack file can never run arbitrary code.
"""
from __future__ import annotations

import ast
from typing import Any

_FUNCS = {"len": len, "min": min, "max": max, "abs": abs, "sum": sum, "any": any,
          "all": all, "round": round, "float": float, "int": int, "str": str, "lower": str.lower}

_BIN = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b if b else 0.0, ast.Mod: lambda a, b: a % b}
_CMP = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
        ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
        ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
        ast.Is: lambda a, b: a is b, ast.IsNot: lambda a, b: a is not b}


class _Ns(dict):
    """Dict that also allows attribute access, so rules can say evidence.oos_days."""

    def __getattr__(self, k: str) -> Any:
        v = self.get(k)
        return _Ns(v) if isinstance(v, dict) else v


def _ev(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _ev(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[node.id]
        v = env.get(node.id)
        return _Ns(v) if isinstance(v, dict) else v
    if isinstance(node, ast.Attribute):
        base = _ev(node.value, env)
        if isinstance(base, dict):
            v = base.get(node.attr)
            return _Ns(v) if isinstance(v, dict) else v
        raise ValueError(f"attribute access not allowed: {node.attr}")
    if isinstance(node, ast.Subscript):
        base = _ev(node.value, env)
        key = _ev(node.slice, env)
        try:
            v = base[key]
        except (KeyError, IndexError, TypeError):
            return None
        return _Ns(v) if isinstance(v, dict) else v
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        vals = [_ev(e, env) for e in node.elts]
        return set(vals) if isinstance(node, ast.Set) else vals
    if isinstance(node, ast.BoolOp):
        vals = (_ev(v, env) for v in node.values)
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp):
        v = _ev(node.operand, env)
        if isinstance(node.op, ast.Not):
            return not v
        if isinstance(node.op, ast.USub):
            return -v
        raise ValueError("unary op not allowed")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        a, b = _ev(node.left, env), _ev(node.right, env)
        if a is None or b is None:
            return None
        return _BIN[type(node.op)](a, b)
    if isinstance(node, ast.Compare):
        left = _ev(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _ev(comp, env)
            try:
                ok = _CMP[type(op)](left, right)
            except TypeError:
                return False
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _ev(node.body, env) if _ev(node.test, env) else _ev(node.orelse, env)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        args = [_ev(a, env) for a in node.args]
        try:
            return _FUNCS[node.func.id](*args)
        except (TypeError, ValueError):
            return None
    raise ValueError(f"expression element not allowed: {type(node).__name__}")


def evaluate(expr: str, env: dict[str, Any]) -> Any:
    return _ev(ast.parse(expr, mode="eval"), env)
