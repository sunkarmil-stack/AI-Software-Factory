"""Normalized enterprise specification contract (REQ 002, Section 8 'Required specification package').

The factory owns this contract; Spec Kit / OpenSpec are adapters, not the governance boundary.
"""
SPEC_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["spec_id", "version", "title", "owner", "risk_tier", "business_outcomes", "actors", "scope",
                 "preconditions", "flows", "contracts", "nfrs", "data_classification", "exclusions",
                 "requirements", "decisions", "dependencies", "assumptions", "permitted_agent_actions",
                 "release_requirements"],
    "properties": {
        "spec_id": {"type": "string", "pattern": "^SPEC-[A-Z0-9-]+$"},
        "version": {"type": "integer", "minimum": 1},
        "title": {"type": "string", "minLength": 3},
        "project": {"type": "string"},
        "owner": {"type": "string", "minLength": 1},
        "risk_tier": {"type": "integer", "minimum": 0, "maximum": 3},
        "business_outcomes": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["id", "statement", "measure"],
            "properties": {"id": {"type": "string", "pattern": "^BO-[0-9]+$"}}}},
        "actors": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["name", "description"]}},
        "scope": {"type": "object", "required": ["in", "out"]},
        "preconditions": {"type": "array", "minItems": 1},
        "flows": {"type": "object", "required": ["normal", "exception"],
                  "properties": {"normal": {"type": "array", "minItems": 1}, "exception": {"type": "array", "minItems": 1}}},
        "contracts": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["id", "kind", "version", "endpoints"],
            "properties": {"endpoints": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["operation_id", "method", "path", "required_scope", "responses"]}}}}},
        "nfrs": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["id", "statement", "measure"]}},
        "data_classification": {"type": "object", "required": ["level", "fields"],
                                "properties": {"level": {"enum": ["public", "internal", "confidential", "restricted"]}}},
        "exclusions": {"type": "array"},
        "requirements": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["id", "statement", "outcome", "acceptance_criteria"],
            "properties": {
                "id": {"type": "string", "pattern": "^REQ-[0-9]+$"},
                "acceptance_criteria": {"type": "array", "minItems": 1, "items": {
                    "type": "object", "required": ["id", "given", "when", "then", "check"],
                    "properties": {"id": {"type": "string", "pattern": "^AC-[0-9]+$"},
                                   "check": {"type": "object", "required": ["request", "expect"]}}}}}}},
        "decisions": {"type": "array", "items": {"type": "object",
                      "required": ["id", "context", "options", "decision", "consequences", "evidence",
                                   "revisit_triggers", "status"]}},
        "dependencies": {"type": "array"},
        "assumptions": {"type": "array", "items": {"type": "object", "required": ["id", "statement", "status"]}},
        "permitted_agent_actions": {"type": "array", "minItems": 1},
        "release_requirements": {"type": "object", "required": ["environments", "rollback"]},
        "history": {"type": "array"},
        "extensions": {"type": "object"},
    },
}

# Fields whose emptiness blocks autonomous execution even if structurally present (REQ 002).
SUBSTANTIVE = ["actors", "preconditions", "flows.normal", "flows.exception", "contracts", "nfrs",
               "data_classification.fields", "requirements", "permitted_agent_actions"]
