"""Agent runtime: one worker type, scoped by manifest (Section 4 'Initial agent and skill catalog').

Each run: verify pinned skills -> assemble versioned prompt -> model gateway -> schema
validation with bounded repair -> explicit success/blocked/failed status with evidence refs
(REQ 010). Any tool request in the model output is executed only through the tool gateway
under the agent's runtime identity; skills and instructions never add permissions (REQ 014).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from ..core.identity import Principal
from ..core.util import digest_obj
from ..models.gateway import ModelContext
from ..tools.gateway import CallContext, ToolDenied

ENVELOPE = {
    "type": "object",
    "required": ["status", "summary", "evidence_refs", "payload"],
    "properties": {
        "status": {"enum": ["success", "blocked", "failed"]},
        "summary": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "payload": {"type": "object"},
        "questions": {"type": "array"},
        "tool_requests": {"type": "array"},
    },
    "additionalProperties": False,
}

REQUIRED_AGENT_MANIFEST = ["id", "version", "owner", "framework", "runtime", "skills", "model", "tools", "state_schema",
                           "memory_policy", "budgets", "termination", "evaluation_suite", "deployment_class",
                           "prompt", "payload_schema"]


class AgentManifestError(ValueError):
    pass


@dataclass
class AgentResult:
    status: str
    summary: str
    payload: dict
    evidence_refs: list[str]
    questions: list
    tool_results: list
    deployment: str
    repaired: bool = False


def load_manifest(path: Path) -> dict:
    m = yaml.safe_load(Path(path).read_text())
    missing = [f for f in REQUIRED_AGENT_MANIFEST if f not in m]
    if missing:
        raise AgentManifestError(f"agent manifest {path} missing {missing}")
    return m


class Agent:
    def __init__(self, factory, manifest: dict):
        self.f = factory
        self.m = manifest
        self.principal: Principal = factory.directory.get(f"agent:{manifest['id']}")
        pid, pv = manifest["prompt"]["id"], manifest["prompt"]["version"]
        pfile = factory.catalog / "prompts" / f"{pid}@{pv}.yaml"
        if not pfile.exists():
            pfile = factory.catalog / "prompts" / f"{pid}.yaml"
        self.prompt = yaml.safe_load(pfile.read_text())
        if str(self.prompt["version"]) != str(manifest["prompt"]["version"]):
            raise AgentManifestError(f"prompt {manifest['prompt']['id']} version drift: pinned "
                                     f"{manifest['prompt']['version']} found {self.prompt['version']}")
        pinned = manifest["prompt"].get("digest")
        if pinned and digest_obj(self.prompt) != pinned:
            raise AgentManifestError(f"prompt {pid}@{pv} content changed outside the release process (digest mismatch)")

    @property
    def bundle_digest(self) -> str:
        return digest_obj({"manifest": self.m, "prompt": self.prompt})

    def run(self, rc, task: dict, *, tenant: str | None = None, env: str = "sandbox",
            purpose: str | None = None, data_class: str = "internal", approval_ids: list[str] | None = None) -> AgentResult:
        skills = [self.f.skills.load(s["name"], str(s["version"]), s.get("digest"), rc.run_id if rc else "adhoc")
                  for s in self.m["skills"]]
        system = "\n\n".join([self.prompt["system"]] + [f"# Skill {s.name}@{s.version}\n{s.instructions}" for s in skills])
        tenant = tenant or (rc.tenant if rc else "default")
        mctx = ModelContext(self.principal, rc.project if rc else "adhoc", tenant, rc.run_id if rc else "adhoc",
                            rc.root_id if rc else "adhoc", purpose or self.m["model"]["purpose"], data_class,
                            on_usage=rc.charge_model if rc else None)
        prompt_ref = {"id": self.prompt["id"], "version": self.prompt["version"], "digest": digest_obj(self.prompt),
                      "skills": {s.name: s.digest for s in skills}, "agent_bundle": self.bundle_digest}
        task = {**task, **({"prompt_config": self.prompt["config"]} if "config" in self.prompt else {})}
        comp = self.f.models.complete(mctx, self.m["model"]["deployment"], system, task, prompt_ref)
        out, repaired = comp.output, False
        errs = self._errors(out)
        if errs:
            # bounded repair: one retry with the validation errors; never trusted to self-declare success
            comp = self.f.models.complete(mctx, self.m["model"]["deployment"], system,
                                          {**task, "repair": {"errors": errs[:5]}}, prompt_ref)
            out, repaired = comp.output, True
            errs = self._errors(out)
            if errs:
                self.f.ledger.record("agent.output_rejected", self.m["id"], self.principal.id,
                                     {"run": rc.run_id if rc else None, "errors": errs[:5]}, mctx.project)
                return AgentResult("failed", "output failed schema validation after bounded repair", {}, [], [], [],
                                   comp.deployment, True)
        tool_results = []
        for tr in out.get("tool_requests", []):
            cctx = CallContext(self.principal, tenant, mctx.project, mctx.run_id, tr.get("env", env),
                               approval_ids or [], "product" if self.m["deployment_class"] == "product" else "delivery",
                               on_call=rc.charge_tool if rc else None)
            try:
                res = self.f.tools.invoke(cctx, tr["tool"], tr["action"], tr.get("args", {}),
                                          op_id=rc.op_id(f"{tr['tool']}.{tr['action']}") if rc else None)
                tool_results.append({"tool": tr["tool"], "action": tr["action"], "ok": True, "result": res})
            except ToolDenied as e:
                tool_results.append({"tool": tr["tool"], "action": tr["action"], "ok": False, "denied": str(e)})
        ev = self.f.ledger.record("agent.output", self.m["id"], self.principal.id,
                                  {"run": rc.run_id if rc else None, "status": out["status"], "prompt": prompt_ref,
                                   "deployment": comp.deployment, "revision": comp.revision,
                                   "payload_digest": digest_obj(out["payload"]), "repaired": repaired,
                                   "tool_results": [{k: v for k, v in t.items() if k != "result"} for t in tool_results],
                                   "limitations": comp.limitations}, mctx.project)
        return AgentResult(out["status"], out["summary"], out["payload"], out["evidence_refs"] + [ev["id"]],
                           out.get("questions", []), tool_results, comp.deployment, repaired)

    def _errors(self, out: Any) -> list[str]:
        if not isinstance(out, dict):
            return ["output is not a JSON object"]
        errs = [e.message for e in jsonschema.Draft202012Validator(ENVELOPE).iter_errors(out)]
        if not errs:
            errs += [f"payload: {e.message}" for e in
                     jsonschema.Draft202012Validator(self.m["payload_schema"]).iter_errors(out["payload"])]
            if out["status"] == "success" and not out["evidence_refs"]:
                errs.append("success claimed without evidence references")
        return errs
