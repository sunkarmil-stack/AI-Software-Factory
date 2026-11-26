"""Business rules and actions in the ontology: safe rules, discovery, governed execution, approvals, impact."""
import copy

import pytest

from factory.core.approvals import ApprovalError
from factory.core.identity import Principal
from factory.core.util import REPO_ROOT
from factory.delivery.release import PromotionDenied
from factory.demo import load_spec
from factory.ontology import rules as R
from factory.ontology.actions import ActionDenied
from factory.ontology.discovery import discover

GLOSSARY = [{"id": "glossary.md", "text": (REPO_ROOT / "samples/ontology/glossary.md").read_text()}]
PROV = {"source": "ops-policy", "evidence": "quoted policy sentence"}
SUBJECT = {"id": "acc-100", "tenant": "retail"}
APPROVERS = (("onto.olivia", "ontology_owner"), ("arch.alex", "architect"), ("po.priya", "product_owner"),
             ("data.dana", "data_owner"), ("sec.sam", "security"))


def approve_all(f, kinds=None):
    for p in f.ontology.proposals("pending"):
        if kinds and p["kind"] not in kinds:
            continue
        for user, role in APPROVERS:
            q = f.ontology.proposal(p["id"])
            if q["status"] == "pending" and role in q["roles"] and user != q["proposer"]:
                f.ontology.approve(p["id"], user, role)


@pytest.fixture
def lending(f):
    """Accounts ontology discovered from the spec + glossary and fully approved."""
    discover(f, load_spec(1), GLOSSARY)
    approve_all(f)
    return f


# ---------------------------------------------------------------- rule engine
def test_rule_engine_is_safe_and_correct():
    assert R.evaluate("amount_cents >= 1 and amount_cents <= 100", {"amount_cents": 50})
    assert not R.evaluate("status in ['open', 'review']", {"status": "closed"})
    assert R.evaluate("len(reason) >= 5 and lower(code) == 'abc'", {"reason": "fraud", "code": "ABC"})
    assert R.names("a > 1 and len(b) < c") == {"a", "b", "c"}
    for bad in ["__import__('os').system('id')", "x.__class__", "(lambda: 1)()", "open('f')", "x[0]",
                "[y for y in range(3)]", "a := 1"]:
        with pytest.raises(R.RuleSyntaxError):
            R.parse(bad)
    with pytest.raises(R.UnknownValue):
        R.evaluate("amount_cents > 0", {})


# ---------------------------------------------------------------- discovery
def test_discovery_proposes_rules_and_actions(f):
    out = discover(f, load_spec(1), GLOSSARY)
    kinds = sorted(p["kind"] for p in out["proposals"])
    assert kinds == ["add_action", "add_concept", "add_concept", "add_rule", "add_rule"]
    rules = {f.ontology.proposal(p["id"])["payload"]["rule"]["id"]: f.ontology.proposal(p["id"])
             for p in out["proposals"] if p["kind"] == "add_rule"}
    rng = rules["R-HOLD-AMOUNT-CENTS-RANGE"]["payload"]["rule"]
    assert rng["expression"] == "amount_cents >= 1 and amount_cents <= 100000000" and rng["severity"] == "block"
    sup = rules["R-HOLD-SUPERVISOR-APPROVAL"]
    assert sup["payload"]["rule"]["expression"] == "amount_cents <= 1000000"
    assert sup["payload"]["rule"]["approval_role"] == "supervisor" and "glossary.md" == sup["provenance"]["source"]
    assert sup["roles"] == ["ontology_owner", "product_owner"]          # business rules are owned by the business
    act = [f.ontology.proposal(p["id"]) for p in out["proposals"] if p["kind"] == "add_action"][0]
    a = act["payload"]["action"]
    assert a["name"] == "CreateHold" and a["creates"] == "Hold" and a["tool"] == {"name": "holds", "action": "create"}
    assert act["target"] == "Account" and act["roles"] == ["ontology_owner", "architect"]


def test_rules_and_actions_wait_for_their_concepts(f):
    discover(f, load_spec(1), GLOSSARY)
    approve_all(f, kinds={"add_rule", "add_action"})          # approved before the concepts exist
    assert all(p["status"] == "pending" and "cannot apply yet" in (p["reason"] or "")
               for p in f.ontology.proposals("pending") if p["kind"] in ("add_rule", "add_action"))
    approve_all(f, kinds={"add_concept"})                     # concepts land -> waiting changes apply
    assert {r["id"] for r in f.ontology.concept("Hold")["rules"]} == {"R-HOLD-AMOUNT-CENTS-RANGE", "R-HOLD-SUPERVISOR-APPROVAL"}
    assert [a["name"] for a in f.ontology.concept("Account")["actions"]] == ["CreateHold"]


# ---------------------------------------------------------------- validation
def test_rule_and_action_validation(lending):
    o = lending.ontology
    bad = o.propose("add_rule", "Hold", {"rule": {"id": "R-HOLD-X", "name": "x", "expression": "credit_score > 600",
                                                  "severity": "block", "message": "m"}}, PROV, "data.dana")
    assert "not attributes of Hold" in bad["validation"][0]
    bad = o.propose("add_rule", "Hold", {"rule": {"id": "R-HOLD-Y", "name": "y", "expression": "__import__('os')",
                                                  "severity": "block", "message": "m"}}, PROV, "data.dana")
    assert "expression rejected" in bad["validation"][0]
    bad = o.propose("add_rule", "Hold", {"rule": {"id": "R-HOLD-Z", "name": "z", "expression": "amount_cents < 5",
                                                  "severity": "require_approval", "message": "m"}}, PROV, "data.dana")
    assert "approval_role" in " ".join(bad["validation"])
    bad = o.propose("add_action", "Account", {"action": {"name": "WireMoney", "description": "d", "creates": "Hold",
                                                         "parameters": [{"name": "iban", "type": "string"}],
                                                         "side_effect": "write", "reversible": True,
                                                         "tool": {"name": "payments", "action": "ledger_write"}}},
                    PROV, "data.dana")
    v = " ".join(bad["validation"])
    assert "not an attribute of Hold" in v and "consequential" in v
    used = o.propose("retire_rule", "Hold", {"rule_id": "R-HOLD-AMOUNT-CENTS-RANGE"}, PROV, "data.dana")
    assert used["status"] == "pending"   # not a precondition of any action, so retirement is allowed (with approval)


def test_consequential_action_needs_security(lending):
    p = lending.ontology.propose("add_action", "Account", {"action": {
        "name": "PostLedgerEntry", "description": "Post a ledger entry for an account", "parameters": [],
        "side_effect": "consequential", "reversible": False, "allowed_roles": ["supervisor"],
        "tool": {"name": "payments", "action": "ledger_write"}}}, PROV, "data.dana")
    assert p["status"] == "pending" and set(p["roles"]) == {"ontology_owner", "architect", "security"}


# ---------------------------------------------------------------- execution
def test_action_executes_through_rules_and_gateway(lending):
    f = lending
    r = f.actions.execute("CreateHold", "ops.omar", SUBJECT, {"amount_cents": 500, "reason": "fraud review"})
    assert r["status"] == "executed" and r["result"]["status"] == "placed"
    assert {x["rule"]: x["outcome"] for x in r["rule_results"]} == {"R-HOLD-AMOUNT-CENTS-RANGE": "pass",
                                                                    "R-HOLD-SUPERVISOR-APPROVAL": "pass"}
    assert f.db.one("SELECT COUNT(*) n FROM holds_sor")["n"] == 1
    call = f.ledger.query(kind="tool.call", subject="holds.create")[-1]
    assert call["actor"] == "ontology-actions"
    assert f.ledger.query(kind="ontology.action.executed")[-1]["actor"] == "ops.omar"   # on behalf of


def test_block_rule_refuses(lending):
    r = lending.actions.execute("CreateHold", "ops.omar", SUBJECT, {"amount_cents": 0, "reason": "x"})
    assert r["status"] == "blocked" and "amount_cents must be" in r["messages"][0]
    assert lending.db.one("SELECT COUNT(*) n FROM holds_sor")["n"] == 0


def test_require_approval_rule_creates_bound_request(lending):
    f = lending
    params = {"amount_cents": 2_500_000, "reason": "court order"}
    r = f.actions.execute("CreateHold", "ops.omar", SUBJECT, params)
    assert r["status"] == "pending_approval" and r["roles"] == ["supervisor"]
    assert f.db.one("SELECT COUNT(*) n FROM holds_sor")["n"] == 0
    with pytest.raises(ActionDenied, match="not required"):
        f.actions.approve(r["request_id"], "rel.rita", "release_owner")
    f.directory.get("ops.omar").roles.add("supervisor")       # even a supervisor cannot approve their own request
    with pytest.raises(ApprovalError, match="independent"):
        f.actions.approve(r["request_id"], "ops.omar", "supervisor")
    f.directory.get("ops.omar").roles.discard("supervisor")
    done = f.actions.approve(r["request_id"], "ops.sue", "supervisor")
    assert done["status"] == "executed"
    again = f.actions.execute("CreateHold", "ops.omar", SUBJECT, params, request_id=r["request_id"])
    assert again["result"]["hold_id"] == done["result"]["hold_id"]   # stable op id: no duplicate hold
    assert f.db.one("SELECT COUNT(*) n FROM holds_sor")["n"] == 1


def test_who_may_run_an_action(lending):
    f = lending
    with pytest.raises(ActionDenied, match="lacks a role"):
        f.actions.execute("CreateHold", "aud.ada", SUBJECT, {"amount_cents": 5, "reason": "abcde"})
    f.directory.add(Principal("agent:servicing", "agent", tenants={"retail"}))
    with pytest.raises(ActionDenied, match="no grant action:CreateHold"):
        f.actions.execute("CreateHold", "agent:servicing", SUBJECT, {"amount_cents": 5, "reason": "abcde"})
    f.directory.get("agent:servicing").grants.append("action:CreateHold@retail/*")
    assert f.actions.execute("CreateHold", "agent:servicing", SUBJECT, {"amount_cents": 5, "reason": "abcde"})["status"] == "executed"
    with pytest.raises(ActionDenied, match="tenant"):
        f.actions.execute("CreateHold", "ops.omar", {"id": "acc-900", "tenant": "wealth"}, {"amount_cents": 5, "reason": "abcde"})
    with pytest.raises(ActionDenied, match="must be money"):
        f.actions.execute("CreateHold", "ops.omar", SUBJECT, {"amount_cents": "5", "reason": "abcde"})


def test_new_rule_applies_immediately_and_retired_action_cannot_run(lending):
    f = lending
    p = f.ontology.propose("add_rule", "Hold", {"rule": {"id": "R-HOLD-REASON-LENGTH", "name": "Meaningful reason",
                                                          "expression": "len(reason) >= 5", "severity": "block",
                                                          "message": "Give a reason of at least 5 characters"}},
                           PROV, "data.dana")
    approve_all(f)
    assert f.ontology.proposal(p["id"])["status"] == "accepted"
    r = f.actions.execute("CreateHold", "ops.omar", SUBJECT, {"amount_cents": 5, "reason": "abc"})
    assert r["status"] == "blocked" and "at least 5" in r["messages"][0]
    f.ontology.propose("retire_action", "Account", {"action_name": "CreateHold"}, PROV, "data.dana")
    approve_all(f)
    with pytest.raises(ActionDenied, match="retired"):
        f.actions.execute("CreateHold", "ops.omar", SUBJECT, {"amount_cents": 5, "reason": "abcde"})


def test_rule_change_invalidates_dependent_release(delivered):
    f, spec, d, r = delivered
    discover(f, spec, GLOSSARY)
    approve_all(f, kinds={"add_concept", "add_action"})
    f.ontology.link_specs()
    assert f.release.evidence_summary(r["release_id"])["stale"] == []
    f.ontology.propose("add_rule", "Hold", {"rule": {"id": "R-HOLD-REASON-LENGTH", "name": "Meaningful reason",
                                                      "expression": "len(reason) >= 5", "severity": "block",
                                                      "message": "Give a reason of at least 5 characters"}},
                       PROV, "data.dana")
    approve_all(f, kinds={"add_rule"})
    assert f.release.evidence_summary(r["release_id"])["stale"]
    fresh = f.approvals.approve("rel.ravi", "release_owner", f.release.binding(r["release_id"], "prod"))
    with pytest.raises(PromotionDenied, match="stale"):
        f.release.promote(r["release_id"], "prod", [fresh], "eng.erin")


def test_spec_check_compares_contract_with_rules(lending):
    spec = load_spec(1)
    assert lending.ontology.check_spec(spec) == []
    loose = copy.deepcopy(spec)
    del loose["contracts"][0]["endpoints"][2]["request_schema"]["properties"]["amount_cents"]["minimum"]
    fs = lending.ontology.check_spec(loose)
    assert any(x.get("ref") == "R-HOLD-AMOUNT-CENTS-RANGE" and "minimum" in x["finding"] for x in fs)


def test_export_includes_rules_and_actions(lending):
    ttl = lending.ontology.export_turtle()
    assert ":R-HOLD-AMOUNT-CENTS-RANGE a :BusinessRule" in ttl and ":CreateHold a :ActionType" in ttl
    assert ":creates :Hold" in ttl
