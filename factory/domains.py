"""Domain packs: onboard a new line of business from reviewed configuration.

catalog/domains/<name>/project.yaml  -> project onboarding (tenant, owners, risk tier, budget, template)
catalog/domains/<name>/ontology.yaml -> concepts, business rules and actions, imported as GOVERNED PROPOSALS
                                        (never seeded): owners must approve each before it takes effect.
Agents, prompts, skills, tool contracts, identities and model routes live in the shared catalog and are
registered by bootstrap like any other catalog entry.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .core.util import REPO_ROOT

DOMAINS = REPO_ROOT / "catalog" / "domains"


def domain_config(name: str) -> dict:
    p = DOMAINS / name / "project.yaml"
    if not p.exists():
        raise FileNotFoundError(f"no domain pack {name} (expected {p})")
    return yaml.safe_load(p.read_text())


def import_ontology(f, path: Path, proposer: str) -> list[dict]:
    doc = yaml.safe_load(Path(path).read_text())
    src, onto, out = doc["source"], f.ontology, []
    pending = {(p["kind"], p["target"], p["payload_digest"]) for p in onto.proposals("pending")}

    def prop(kind, target, payload, evidence):
        from .core.util import digest_obj
        if (kind, target or payload.get("name"), digest_obj(payload)) in pending:
            return
        out.append(onto.propose(kind, target, payload, {"source": src, "evidence": evidence, "method": "domain-pack"},
                                proposer))

    for c in doc.get("concepts", []):
        if not onto.concept(c["name"]):
            prop("add_concept", None, {k: v for k, v in c.items() if k != "evidence"}, c["evidence"])
    rules, actions = onto.all_rules(), onto.all_actions()
    for r in doc.get("rules", []):
        if r["rule"]["id"] not in rules:
            prop("add_rule", r["target"], {"rule": r["rule"]}, r["evidence"])
    for a in doc.get("actions", []):
        if a["action"]["name"] not in actions:
            prop("add_action", a["target"], {"action": a["action"]}, a["evidence"])
    return out


def load_domain(f, name: str, actor: str = "admin.pat") -> dict:
    cfg = domain_config(name)
    if not f.db.one("SELECT id FROM projects WHERE id=?", (cfg["project"],)):
        f.onboard(cfg["project"], cfg["tenant"], cfg["owners"], risk_tier=cfg["risk_tier"], budget_usd=cfg["budget_usd"],
                  template=cfg.get("template", "fastapi-service"), actor=actor)
    props = import_ontology(f, DOMAINS / name / cfg["ontology"], actor)
    invalid = [p for p in props if p["status"] == "invalid"]
    f.ledger.record("domain.loaded", name, actor, {"project": cfg["project"], "proposals": [p["id"] for p in props],
                                                   "invalid": [p["id"] for p in invalid]}, cfg["project"])
    return {"project": cfg["project"], "tenant": cfg["tenant"], "proposals": props, "invalid": invalid}
