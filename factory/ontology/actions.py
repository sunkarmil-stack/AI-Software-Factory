"""Ontology actions: the only governed way for people and agents to change business objects.

execute(action, actor, subject, params):
  1. the action exists and is active (retired actions cannot run)
  2. the actor may run it: a human holding one of `allowed_roles`, or an agent/workload with
     the grant `action:<Name>@<tenant>/*` in catalog/identities.yaml
  3. parameters are type-checked against the action definition
  4. business rules are evaluated deterministically:
       - rules of the concept the action creates (on the parameters)
       - rules of the subject concept (on the subject's attributes)
       - the action's explicit preconditions (on subject + parameters)
     `block` -> refused; a rule that cannot be decided (missing data) also refuses (fail closed);
     `warn` -> recorded; `require_approval` -> a pending request that a human with the named role
     must approve, bound to this exact action, subject, parameters and environment
  5. the bound tool is invoked through the tool gateway by the `ontology-actions` workload
     on behalf of the actor, with a stable operation id (no duplicate writes on retry)
"""
from __future__ import annotations

from ..core.approvals import ApprovalError, ApprovalService
from ..core.db import j, unj
from ..core.util import digest_obj, new_id, now
from ..tools.gateway import CallContext, ToolDenied
from . import rules as rule_engine

SCHEMA = """CREATE TABLE IF NOT EXISTS action_requests(id TEXT PRIMARY KEY, action TEXT, actor TEXT, subject TEXT,
  params TEXT, env TEXT, tenant TEXT, status TEXT, roles TEXT, approvals TEXT, rule_results TEXT, result TEXT,
  created REAL, decided REAL)"""
PY_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "money": int,
            "identifier": str, "enum": str, "date": str, "datetime": str}


class ActionDenied(PermissionError):
    pass


class ActionService:
    WORKLOAD = "ontology-actions"

    def __init__(self, f):
        self.f = f
        f.db.execute(SCHEMA)

    # ------------------------------------------------------------ rule evaluation
    def evaluate_rules(self, action: dict, subject: dict, params: dict) -> list[dict]:
        o = self.f.ontology
        checks: list[tuple[dict, dict, str]] = []
        if action.get("creates"):
            for r in (o.concept(action["creates"]) or {}).get("rules", []):
                checks.append((r, params, action["creates"]))
        for r in (o.concept(action["concept"]) or {}).get("rules", []):
            if set(rule_engine.names(r["expression"])) & set(subject):
                checks.append((r, subject, action["concept"]))
        rules = o.all_rules()
        for rid in action.get("preconditions", []):
            if rid in rules:
                checks.append((rules[rid], {**subject, **params}, rules[rid]["concept"]))
        out, seen = [], set()
        for r, data, scope in checks:
            if r.get("status", "active") != "active" or r["id"] in seen:
                continue
            seen.add(r["id"])
            try:
                ok = rule_engine.evaluate(r["expression"], data)
                outcome = "pass" if ok else "fail"
            except rule_engine.UnknownValue as e:
                outcome, ok = "unknown", False
                r = {**r, "message": f"{r['message']} (cannot be decided: '{e.args[0]}' not provided)"}
            except rule_engine.RuleSyntaxError as e:
                outcome, ok = "error", False
                r = {**r, "message": f"{r['message']} ({e})"}
            out.append({"rule": r["id"], "name": r["name"], "scope": scope, "severity": r["severity"],
                        "outcome": outcome, "message": r["message"], "approval_role": r.get("approval_role")})
        return out

    # ------------------------------------------------------------ authorization
    def _authorized(self, action: dict, actor_id: str, tenant: str) -> str | None:
        p = self.f.directory.get(actor_id)
        if not p.in_tenant(tenant):
            return f"{actor_id} has no access to tenant {tenant}"
        if p.kind == "human":
            if not set(action.get("allowed_roles", [])) & p.roles:
                return f"{actor_id} lacks a role allowed to run {action['name']} ({action.get('allowed_roles', [])})"
            return None
        if not p.can("action", action["name"], f"{tenant}/ontology"):
            return f"{actor_id} has no grant action:{action['name']} (skills or prompts never grant it)"
        return None

    def _typecheck(self, action: dict, params: dict) -> list[str]:
        errs, spec = [], {p["name"]: p for p in action.get("parameters", [])}
        for n, p in spec.items():
            if p.get("required") and n not in params:
                errs.append(f"missing parameter {n}")
        for n, v in params.items():
            if n not in spec:
                errs.append(f"unknown parameter {n}")
            elif not isinstance(v, PY_TYPES.get(spec[n]["type"], str)) or (spec[n]["type"] in ("integer", "money") and isinstance(v, bool)):
                errs.append(f"parameter {n} must be {spec[n]['type']}")
        return errs

    def binding(self, action: str, subject: dict, params: dict, env: str) -> dict:
        return ApprovalService.binding(f"ontology.action:{action}", digest_obj({"subject": subject, "params": params}),
                                       env, action)

    # ------------------------------------------------------------ execute
    def execute(self, action_name: str, actor_id: str, subject: dict, params: dict, env: str = "nonprod",
                tenant: str | None = None, request_id: str | None = None) -> dict:
        action = self.f.ontology.all_actions().get(action_name)
        if not action:
            raise ActionDenied(f"action {action_name} does not exist or is retired")
        tenant = tenant or subject.get("tenant") or "retail"
        why = self._authorized(action, actor_id, tenant)
        if why:
            self._record("ontology.action.denied", action_name, actor_id, {"reason": why})
            raise ActionDenied(why)
        errs = self._typecheck(action, params)
        if errs:
            raise ActionDenied("; ".join(errs))
        results = self.evaluate_rules(action, subject, params)
        blocking = [r for r in results if r["severity"] == "block" and r["outcome"] != "pass"] + \
                   [r for r in results if r["outcome"] in ("unknown", "error") and r["severity"] != "warn"]
        warnings = [r for r in results if r["severity"] == "warn" and r["outcome"] != "pass"]
        if blocking:
            self._record("ontology.action.blocked", action_name, actor_id,
                         {"rules": [b["rule"] for b in blocking], "subject": subject.get("id")})
            return {"status": "blocked", "action": action_name, "rule_results": results,
                    "messages": [b["message"] for b in blocking]}
        roles = sorted({r["approval_role"] for r in results if r["severity"] == "require_approval" and r["outcome"] == "fail"})
        req = self._request(request_id, action_name, actor_id, subject, params, env, tenant, roles, results)
        if roles:
            try:
                used = self.f.approvals.require(req["approvals"], self.binding(action_name, subject, params, env), roles)
            except ApprovalError:
                return {"status": "pending_approval", "action": action_name, "request_id": req["id"], "roles": roles,
                        "rule_results": results, "messages": [r["message"] for r in results
                                                              if r["severity"] == "require_approval" and r["outcome"] == "fail"]}
        else:
            used = []
        result = self._perform(action, actor_id, subject, params, env, tenant, req["id"])
        self.f.db.execute("UPDATE action_requests SET status='executed', result=?, decided=? WHERE id=?",
                          (j(result), now(), req["id"]))
        self._record("ontology.action.executed", action_name, actor_id,
                     {"request": req["id"], "subject": subject.get("id"), "params_digest": digest_obj(params),
                      "approvals": used, "warnings": [w["rule"] for w in warnings], "env": env})
        return {"status": "executed", "action": action_name, "request_id": req["id"], "result": result,
                "rule_results": results, "warnings": [w["message"] for w in warnings]}

    def _request(self, rid, action, actor, subject, params, env, tenant, roles, results) -> dict:
        if rid:
            return self.request(rid)
        same = self.f.db.one("SELECT id FROM action_requests WHERE action=? AND actor=? AND subject=? AND params=? AND env=?"
                             " AND status='pending'", (action, actor, j(subject), j(params), env))
        if same:
            return self.request(same["id"])
        rid = new_id("ACT")
        self.f.db.execute("INSERT INTO action_requests(id,action,actor,subject,params,env,tenant,status,roles,approvals,"
                          "rule_results,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (rid, action, actor, j(subject), j(params), env, tenant, "pending", j(roles), j([]), j(results), now()))
        return self.request(rid)

    def _perform(self, action: dict, actor_id: str, subject: dict, params: dict, env: str, tenant: str, rid: str) -> dict:
        tool = action.get("tool")
        if not tool:
            return {"recorded": True, "note": "action has no tool binding; recorded as a governed business event"}
        wl = self.f.directory.get(self.WORKLOAD)
        ctx = CallContext(wl, tenant, "ontology", rid, env, purpose="product")
        args = {**params, **({"subject_id": subject["id"]} if "id" in subject else {}), "tenant": tenant}
        try:
            return self.f.tools.invoke(ctx, tool["name"], tool["action"], args, resource=f"{tenant}/ontology",
                                       op_id=f"action:{rid}")
        except ToolDenied as e:
            raise ActionDenied(f"tool gateway denied {tool['name']}.{tool['action']}: {e}")

    # ------------------------------------------------------------ approvals
    def request(self, rid: str) -> dict:
        r = self.f.db.one("SELECT * FROM action_requests WHERE id=?", (rid,))
        if not r:
            raise KeyError(rid)
        for k in ("subject", "params", "roles", "approvals", "rule_results", "result"):
            r[k] = unj(r[k])
        return r

    def requests(self, status: str | None = None) -> list[dict]:
        rows = self.f.db.all("SELECT id FROM action_requests" + (" WHERE status=?" if status else "") + " ORDER BY created",
                             (status,) if status else ())
        return [self.request(r["id"]) for r in rows]

    def approve(self, rid: str, approver: str, role: str) -> dict:
        r = self.request(rid)
        if r["status"] != "pending":
            raise ActionDenied(f"request {rid} is {r['status']}")
        if role not in r["roles"]:
            raise ActionDenied(f"role {role} is not required for {rid} (requires {r['roles']})")
        aid = self.f.approvals.approve(approver, role, self.binding(r["action"], r["subject"], r["params"], r["env"]),
                                       author_ids={r["actor"]})
        self.f.db.execute("UPDATE action_requests SET approvals=? WHERE id=?", (j(r["approvals"] + [aid]), rid))
        got = {self.f.approvals.get(a)["role"] for a in r["approvals"] + [aid]}
        if set(r["roles"]) <= got:
            return self.execute(r["action"], r["actor"], r["subject"], r["params"], r["env"], r["tenant"], request_id=rid)
        return {"status": "pending_approval", "request_id": rid, "roles": r["roles"]}

    def reject(self, rid: str, actor: str, reason: str) -> dict:
        r = self.request(rid)
        p = self.f.directory.get(actor)
        if p.kind != "human" or not set(r["roles"]) & p.roles:
            raise ActionDenied("only a required approver can reject")
        self.f.db.execute("UPDATE action_requests SET status='rejected', result=?, decided=? WHERE id=?",
                          (j({"reason": reason, "by": actor}), now(), rid))
        self._record("ontology.action.rejected", r["action"], actor, {"request": rid, "reason": reason})
        return self.request(rid)

    def _record(self, kind: str, subject: str, actor: str, data: dict) -> None:
        self.f.ledger.record(kind, subject, actor, data, "ontology")
