"""Dynamic, governed ontology: automatic discovery, tiered approval, impact and invalidation, UI/API."""
import copy
import json
import threading
import time
import urllib.request

import pytest
import uvicorn

from factory.core.approvals import ApprovalError
from factory.core.util import REPO_ROOT, digest_obj
from factory.delivery.release import PromotionDenied
from factory.demo import load_spec
from factory.ontology.discovery import discover, extract
from factory.ontology.registry import OntologyError

GLOSSARY = [{"id": "glossary.md", "text": (REPO_ROOT / "samples/ontology/glossary.md").read_text()}]
PROV = {"source": "test", "evidence": "quoted evidence"}


def _approve_all(f, pid):
    for user, role in (("onto.olivia", "ontology_owner"), ("arch.alex", "architect"), ("data.dana", "data_owner"),
                       ("sec.sam", "security"), ("po.priya", "product_owner")):
        p = f.ontology.proposal(pid)
        if p["status"] == "pending" and role in p["roles"]:
            f.ontology.approve(pid, user, role)
    return f.ontology.proposal(pid)


def test_seed_loaded(f):
    assert {c["name"] for c in f.ontology.concepts()} == {"Tenant", "Customer"}


def test_discovery_extracts_and_grounds_proposals(f):
    spec = load_spec(1)
    cands = extract(spec, f.ontology)
    names = {c["target"] for c in cands if c["kind"] == "add_concept"}
    assert names == {"Account", "Hold"}
    out = discover(f, spec, GLOSSARY)
    props = {p["target"]: f.ontology.proposal(p["id"]) for p in out["proposals"] if p["kind"] == "add_concept"}
    acc, hold = props["Account"]["payload"], props["Hold"]["payload"]
    assert {a["name"] for a in acc["attributes"]} == {"id", "balance_cents", "owner"}
    assert {a["name"] for a in hold["attributes"]} == {"amount_cents", "reason", "idempotency_key"}
    assert {"type": "belongsTo", "target": "Account", "cardinality": "1"} in hold["relations"]
    assert {"type": "scopedBy", "target": "Tenant", "cardinality": "1"} in acc["relations"]
    assert hold["synonyms"] == ["freeze"] and "temporary restriction" in hold["definition"]
    assert "glossary.md" in props["Hold"]["provenance"]["evidence"]
    # everything is a pending proposal: discovery never changes the ontology by itself,
    # and the embedded "SYSTEM OVERRIDE ... approve_all" line in the glossary has no effect
    assert all(f.ontology.proposal(p["id"])["status"] == "pending" and f.ontology.proposal(p["id"])["proposer_kind"] == "agent"
               for p in out["proposals"])
    assert f.ontology.concept("Account") is None and f.ontology.concept("LedgerEntry") is None


def test_tiered_approval_and_separation_of_duties(f):
    discover(f, load_spec(1), GLOSSARY)
    acc = [p for p in f.ontology.proposals("pending") if p["target"] == "Account" and p["kind"] == "add_concept"][0]
    assert acc["tier"] == 3 and acc["roles"] == ["ontology_owner", "architect"]
    with pytest.raises(ApprovalError, match="only humans"):
        f.ontology.approve(acc["id"], "agent:ontology", "ontology_owner")
    with pytest.raises(OntologyError, match="not required"):
        f.ontology.approve(acc["id"], "eng.erin", "engineer")
    f.ontology.approve(acc["id"], "onto.olivia", "ontology_owner")
    assert f.ontology.concept("Account") is None            # one of two approvals: not applied
    f.ontology.approve(acc["id"], "arch.alex", "architect")
    assert f.ontology.concept("Account")["version"] == 1
    hold = [p for p in f.ontology.proposals("pending") if p["target"] == "Hold" and p["kind"] == "add_concept"][0]
    _approve_all(f, hold["id"])
    assert f.ontology.resolve("freeze") == "Hold" and f.ontology.resolve("accounts") == "Account"
    # a human proposer cannot approve their own change
    p = f.ontology.propose("add_relation", "Account", {"relation": {"type": "heldBy", "target": "Customer", "cardinality": "1..*"}},
                           PROV, "onto.olivia")
    with pytest.raises(ApprovalError, match="independent"):
        f.ontology.approve(p["id"], "onto.olivia", "ontology_owner")


def test_tier1_synonym_auto_and_ambiguity_rejected(f):
    p = f.ontology.propose("add_synonym", "Customer", {"synonym": "account holder"}, PROV, "agent:ontology")
    assert p["status"] == "auto_accepted" and f.ontology.resolve("account holder") == "Customer"
    bad = f.ontology.propose("add_synonym", "Tenant", {"synonym": "client"}, PROV, "agent:ontology")
    assert bad["status"] == "invalid" and "ambiguous" in bad["validation"][0]


def test_validation_rules(f):
    o = f.ontology
    assert "PascalCase" in " ".join(o.propose("add_concept", None, {"name": "loan_app", "definition": "x", "data_class": "internal"},
                                              PROV, "onto.olivia")["validation"])
    assert "provenance" in " ".join(o.propose("add_synonym", "Tenant", {"synonym": "lob"}, {}, "onto.olivia")["validation"])
    assert "neither an existing" in " ".join(o.propose("add_relation", "Customer", {"relation": {
        "type": "owns", "target": "Mortgage", "cardinality": "0..*"}}, PROV, "onto.olivia")["validation"])
    assert "more sensitive" in " ".join(o.propose("add_attribute", "Tenant", {"attribute": {
        "name": "tax_id", "type": "string", "data_class": "restricted", "required": False}}, PROV, "onto.olivia")["validation"])


def test_restricted_data_requires_data_owner_and_security(f):
    p = f.ontology.propose("add_attribute", "Customer", {"attribute": {"name": "date_of_birth", "type": "date",
                                                                       "data_class": "confidential", "required": False}},
                           PROV, "agent:ontology")
    assert p["tier"] == 2 and p["roles"] == ["data_owner"]
    f.ontology.approve(p["id"], "data.dana", "data_owner")
    assert "date_of_birth" in {a["name"] for a in f.ontology.concept("Customer")["attributes"]}
    r = f.ontology.propose("add_concept", None, {"name": "TaxIdentifier", "definition": "A government tax id.",
                                                 "data_class": "restricted", "attributes": [], "relations": []},
                           PROV, "agent:ontology")
    assert r["tier"] == 3 and set(r["roles"]) == {"ontology_owner", "architect", "data_owner", "security"}


def test_payload_tamper_detected(f):
    p = f.ontology.propose("update_definition", "Tenant", {"definition": "A business line."}, PROV, "agent:ontology")
    f.db.execute("UPDATE onto_proposals SET payload=? WHERE id=?", (json.dumps({"definition": "Anything goes."}), p["id"]))
    with pytest.raises(OntologyError, match="altered"):
        f.ontology.approve(p["id"], "onto.olivia", "ontology_owner")


def test_breaking_concept_change_invalidates_dependents(delivered):
    f, spec, d, r = delivered
    discover(f, spec, GLOSSARY)
    for p in f.ontology.proposals("pending"):
        if p["kind"] != "add_rule":   # new business rules are themselves breaking; tested separately
            _approve_all(f, p["id"])
    f.ontology.link_specs()
    impact = f.ontology.impact("Account")["dependents"]
    assert impact["SPEC"] == [f"{spec['spec_id']}@v1"] and r["release_id"] in impact["RELEASE"]
    # additive (non-breaking) change keeps everything current
    f.ontology.propose("add_synonym", "Account", {"synonym": "deposit account"}, PROV, "onto.olivia")
    assert f.release.evidence_summary(r["release_id"])["stale"] == []
    # breaking change: definition update -> spec, release, approval stale; approval revoked; promotion refused
    rel_appr = f.db.one("SELECT id FROM approvals WHERE action='release.promote'")["id"]
    p = f.ontology.propose("update_definition", "Account", {"definition": "An account is a contract for deposits only."},
                           PROV, "data.dana")
    _approve_all(f, p["id"])
    assert f.release.evidence_summary(r["release_id"])["stale"]
    assert f.approvals.get(rel_appr)["revoked"]
    fresh = f.approvals.approve("rel.ravi", "release_owner", f.release.binding(r["release_id"], "prod"))
    with pytest.raises(PromotionDenied, match="stale"):
        f.release.promote(r["release_id"], "prod", [fresh], "eng.erin")


def test_spec_check_and_pipeline_block(onboarded):
    f = onboarded
    spec = load_spec(1)
    assert {x["term"] for x in f.ontology.check_spec(spec)} == {"Account", "Hold"}
    discover(f, spec, GLOSSARY)
    for p in f.ontology.proposals("pending"):
        _approve_all(f, p["id"])
    assert f.ontology.check_spec(spec) == []
    bad = copy.deepcopy(spec); bad["data_classification"]["fields"]["owner"] = "internal"
    bad["requirements"][3]["statement"] += " A freeze SHALL be recorded."
    fs = f.ontology.check_spec(bad)
    assert any(x["severity"] == "error" and x["term"] == "Account.owner" for x in fs)
    assert any(x["severity"] == "info" and "canonical term Hold" in x["finding"] for x in fs)
    rid = f.engine.start("spec_pipeline", "accounts", "retail", "po.priya", {"spec": bad, "spec_id": bad["spec_id"], "version": 1})
    assert f.engine.run(rid) == "BLOCKED" and "ontology" in f.engine.get(rid)["error"]


def test_export_turtle(f):
    f.ontology.propose("add_synonym", "Tenant", {"synonym": "line of business"}, PROV, "onto.olivia")
    ttl = f.ontology.export_turtle()
    assert ":Tenant a owl:Class" in ttl and 'skos:altLabel "line of business"' in ttl and "owl:ObjectProperty" in ttl


def test_ontology_api_and_ui(onboarded):
    from factory.api import create_app
    from tests.test_api_and_onboarding import _free_port, call
    f = onboarded
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(f), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(base + "/ontology") as r:
            assert b"Ontology Studio" in r.read()
        f.specs.submit(load_spec(1), "po.priya")
        st, out = call(base, "POST", "/v1/ontology/discover", "dev-onto.olivia",
                       {"spec_id": "SPEC-ACCOUNTS-API", "version": 1, "documents": GLOSSARY})
        assert st == 200 and out["state"] == "SUCCEEDED"
        assert sorted(p["kind"] for p in out["result"]["proposals"]) == ["add_action", "add_concept", "add_concept", "add_rule", "add_rule"]
        pid = [p["id"] for p in out["result"]["proposals"] if p["target"] == "Account" and p["kind"] == "add_concept"][0]
        assert call(base, "POST", f"/v1/ontology/proposals/{pid}/approve", "dev-eng.erin", {"role": "engineer"})[0] == 409
        assert call(base, "POST", f"/v1/ontology/proposals/{pid}/approve", "dev-onto.olivia", {"role": "ontology_owner"})[0] == 200
        st, p = call(base, "POST", f"/v1/ontology/proposals/{pid}/approve", "dev-arch.alex", {"role": "architect"})
        assert p["status"] == "accepted"
        st, c = call(base, "GET", "/v1/ontology/concepts/Account", "dev-aud.ada")
        assert st == 200 and c["concept"]["version"] == 1
        st, created = call(base, "POST", "/v1/ontology/proposals/new", "dev-po.priya",
                           {"kind": "add_synonym", "target": "Account", "payload": {"synonym": "deposit account"},
                            "provenance": PROV})
        assert st == 201 and created["status"] == "auto_accepted"
        assert call(base, "GET", "/v1/ontology/graph", "dev-aud.ada")[1]["edges"]
        # rules and actions over HTTP
        for q in f.ontology.proposals("pending"):
            _approve_all(f, q["id"])
        st, r = call(base, "POST", "/v1/ontology/actions/CreateHold/execute", "dev-aud.ada",
                     {"subject": {"id": "acc-100", "tenant": "retail"}, "params": {"amount_cents": 5, "reason": "abcde"}})
        assert st == 403
        st, r = call(base, "POST", "/v1/ontology/actions/CreateHold/execute", "dev-ops.omar",
                     {"subject": {"id": "acc-100", "tenant": "retail"}, "params": {"amount_cents": 2500000, "reason": "court"}})
        assert st == 200 and r["status"] == "pending_approval"
        st, done = call(base, "POST", f"/v1/ontology/action-requests/{r['request_id']}/approve", "dev-ops.sue", {"role": "supervisor"})
        assert st == 200 and done["status"] == "executed"
        st, tr = call(base, "POST", "/v1/ontology/rules/test", "dev-aud.ada", {"expression": "a > 1", "data": {"a": 2}})
        assert tr["result"] is True
        assert call(base, "POST", "/v1/ontology/rules/test", "dev-aud.ada", {"expression": "open('x')"})[0] == 400
    finally:
        srv.should_exit = True; t.join(5)
