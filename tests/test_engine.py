"""Engine unit tests: no pack run needed."""
from ontoforge.expr import evaluate
from ontoforge.ontology.build import normalize
from ontoforge.policy import PolicyEngine
from ontoforge.tracing import Tracer


def test_expr_basic_and_safe():
    env = {"evidence": {"stock": {"summary": {"oos_days": 6}}}, "params": {"qty": 120}}
    assert evaluate("evidence.stock.summary.oos_days >= 5 and params.qty <= 200", env)
    assert evaluate("max(12, 30 / 2)", {}) == 15
    for bad in ("__import__('os')", "open('x')", "().__class__", "lambda: 1"):
        try:
            evaluate(bad, {})
            raise AssertionError(f"should have refused {bad}")
        except ValueError:
            pass


def test_normalize_identity_keys():
    assert normalize("00123456789012", "digits") == normalize("123456789012", "digits")
    assert normalize("41-2290871", "digits") == "412290871"
    assert normalize("Lumen Skin Company, Inc.", "name") == "lumen skin"
    assert normalize("LUMEN SKIN CO", "name") == "lumen skin"


def test_policy_forbid_beats_permit_and_default_deny():
    pe = PolicyEngine({"rules": [
        {"id": "p", "effect": "permit", "principal": "agent:*", "action": "action:*"},
        {"id": "f", "effect": "forbid", "principal": "agent:*", "action": "action:close_alert"},
        {"id": "c", "effect": "permit", "principal": "agent:x", "action": "action:pay", "when": "params.amount <= 5000"},
    ]})
    assert not pe.authorize("agent:x", "action:close_alert").allowed
    assert pe.authorize("agent:x", "action:escalate").allowed
    assert not pe.authorize("user:y", "action:escalate").allowed           # default deny
    pe2 = PolicyEngine({"rules": [pe.rules[2]]})
    assert pe2.authorize("agent:x", "action:pay", {"params": {"amount": 100}}).allowed
    assert not pe2.authorize("agent:x", "action:pay", {"params": {"amount": 9000}}).allowed


def test_tracer_nests_and_redacts(tmp_path):
    t = Tracer(tmp_path)
    with t.span("outer", dob="1970-01-01") as o:
        with t.span("inner") as i:
            i.event("retry", attempt=1)
    assert i.parent_id == o.span_id and i.trace_id == o.trace_id
    assert o.attributes["dob"] == "[redacted]"
    assert (tmp_path / f"{o.trace_id}.jsonl").exists()
