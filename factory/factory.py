"""Composition root: wires the control plane, delivery plane, trusted build/release plane
and shared controlled services (Section 4 'Architectural layers')."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from .agents import offline  # noqa: F401  (registers offline handlers)
from .ontology import discovery as _onto_discovery  # noqa: F401  (registers ontology agent handler)
from .ontology.registry import Ontology
from .agents.runtime import Agent, load_manifest
from .context.memory import MemoryStore
from .context.retrieval import RetrievalService
from .core.approvals import ApprovalService
from .core.controls import Controls
from .core.crypto import TRUSTED_SIGNERS, KeyStore
from .core.db import DB, j, unj
from .core.evidence import Ledger, ObjectStore
from .core.identity import Directory
from .core.policy import PolicyEngine
from .core.telemetry import Telemetry
from .core.util import REPO_ROOT, digest_obj, factory_home, now
from .delivery.ci import TrustedCI
from .delivery.release import ReleaseService
from .delivery.repo import RepoService
from .delivery.sandbox import SandboxService
from .models.gateway import ModelGateway
from .orchestrator.engine import Engine
from .services import PaymentsLedger, build_impls
from .skills.registry import SkillRegistry
from .specs.registry import SpecRegistry
from .specs.trace import Trace
from .tools.gateway import ToolGateway


class PolicyLoadError(RuntimeError):
    pass


class Factory:
    def __init__(self, home: Path | None = None, catalog: Path | None = None, worker_id: str | None = None):
        self.home = Path(home or factory_home())
        self.home.mkdir(parents=True, exist_ok=True)
        self.catalog = Path(catalog or REPO_ROOT / "catalog")
        self.templates = REPO_ROOT / "templates"
        self.db = DB(self.home / "factory.db")
        self.keys = KeyStore(self.home / "keys")
        for s in TRUSTED_SIGNERS:
            self.keys.ensure(s)
        self.objects = ObjectStore(self.home / "objects")
        self.ledger = Ledger(self.db, self.objects)
        self.tm = Telemetry(self.db)
        self.controls = Controls(self.db, self.ledger)
        self.directory = Directory.load(self.catalog / "identities.yaml")
        self.policy = self._load_policy()
        self.approvals = ApprovalService(self.db, self.keys, self.directory, self.ledger)
        self.trace = Trace(self.db)
        self.specs = SpecRegistry(self.db, self.ledger, self.trace, self.approvals)
        self.ontology = Ontology(self)
        caps = set(yaml.safe_load((self.catalog / "capabilities.yaml").read_text())["capabilities"])
        self.skills = SkillRegistry(self.db, self.ledger, self.keys, self.approvals, self.home / "skill-store", caps)
        self.tools = ToolGateway(self.db, self.ledger, self.policy, self.controls, self.approvals, self.tm)
        self.models = ModelGateway.load(self.db, self.ledger, self.controls, self.tm, self.catalog / "models.yaml")
        self.retrieval = RetrievalService(self.db, self.ledger)
        self.memory = MemoryStore(self.db, self.ledger)
        self.sandboxes = SandboxService(self.home / "sandboxes", self.ledger,
                                        allow_dev_isolation=os.environ.get("FACTORY_ALLOW_DEV_ISOLATION", "1") == "1")
        self.repo = RepoService(self.db, self.ledger, self.home / "repos")
        self.ci = TrustedCI(self)
        self.release = ReleaseService(self)
        self.payments = PaymentsLedger(self.home / "payments-ledger.db")
        self.secret_store = {"prod-db-password": "s3cr3t-prod-value-never-in-sandbox"}
        impls = build_impls(self)
        for p in sorted((self.catalog / "tools").glob("*.yaml")):
            c = yaml.safe_load(p.read_text())
            self.tools.register(c, impls[c["name"]], actor="platform-bootstrap")
        from .ontology.actions import ActionService
        self.actions = ActionService(self)
        self.engine = Engine(self.db, self.ledger, self.tm, worker_id)
        self.engine.f = self
        self.engine.approval_resolver = self.approvals.require
        from .workflows import register_all
        register_all(self)
        self._agents: dict[str, Agent] = {}

    # ---------------- policy (REQ 047) ----------------
    def _load_policy(self) -> PolicyEngine:
        pe = PolicyEngine.load(self.catalog / "policy" / "factory-core.yaml")
        tests = yaml.safe_load((self.catalog / "policy" / "factory-core.tests.yaml").read_text())
        failures = pe.run_tests(tests)
        if failures:
            raise PolicyLoadError(f"policy {pe.policy_id}@{pe.version} failed its test suite: {failures}")
        return pe

    # ---------------- bootstrap ----------------
    def bootstrap(self) -> dict:
        """Publish catalog skills (security review for medium risk) and register agent bundles with pinned digests."""
        published = []
        for pkg in sorted((self.catalog / "skills").iterdir()):
            m = yaml.safe_load((pkg / "skill.yaml").read_text())
            appr = None
            if m["risk"] in ("medium", "high"):
                from .core.util import digest_dir
                appr = self.approvals.approve("sec.sam", "security", ApprovalService.binding(
                    "skill.publish", digest_dir(pkg), "registry", f"{m['name']}@{m['version']}"),
                    rationale="catalog security review")
            published.append(self.skills.publish(pkg, "admin.pat", appr)["name"])
        bundles = []
        for p in sorted((self.catalog / "agents").glob("*.yaml")):
            m = load_manifest(p)
            existing = self.db.one("SELECT * FROM agents WHERE id=? AND version=?", (m["id"], m["version"]))
            if existing:
                continue
            for s in m["skills"]:
                r = self.db.one("SELECT digest FROM skills WHERE name=? AND version=?", (s["name"], str(s["version"])))
                if not r:
                    raise ValueError(f"agent {m['id']} pins unknown skill {s['name']}@{s['version']}")
                s["digest"] = r["digest"]
            pv = self.catalog / "prompts" / f"{m['prompt']['id']}.yaml"
            m["prompt"]["digest"] = digest_obj(yaml.safe_load(pv.read_text()))
            d = digest_obj(m)
            self.db.execute("INSERT INTO agents(id,version,digest,manifest,status) VALUES(?,?,?,?,?)",
                            (m["id"], m["version"], d, j(m), "approved"))
            self.ledger.record("agent.registered", f"{m['id']}@{m['version']}", "admin.pat", {"digest": d})
            bundles.append(m["id"])
        seeded = self.ontology.seed(self.catalog / "ontology" / "seed.yaml")
        return {"skills": published, "agents": bundles, "ontology_seed": seeded}

    def agent(self, role: str) -> Agent:
        if role not in self._agents:
            r = self.db.one("SELECT * FROM agents WHERE id=? AND status='approved' ORDER BY version DESC LIMIT 1", (role,))
            if not r:
                raise KeyError(f"agent bundle {role} not registered/approved")
            m = unj(r["manifest"])
            if digest_obj(m) != r["digest"]:
                raise ValueError(f"agent bundle {role} manifest altered")
            self._agents[role] = Agent(self, m)
        return self._agents[role]

    # ---------------- onboarding (REQ 056) ----------------
    def onboard(self, project: str, tenant: str, owners: dict, risk_tier: int, budget_usd: float,
                template: str = "fastapi-service", skills: list[str] | None = None, actor: str = "admin.pat") -> dict:
        admin = self.directory.get(actor)
        if not admin.has_role("platform_admin"):
            raise PermissionError("onboarding requires platform_admin")
        required = {"product_owner", "architect", "engineer", "qa_owner", "release_owner"}
        missing = required - set(owners)
        if missing:
            raise ValueError(f"accountable owners missing for {sorted(missing)}")
        for role, who in owners.items():
            if not self.directory.get(who).has_role(role):
                raise ValueError(f"{who} does not hold {role}")
        skills = skills or [s["name"] for s in self.skills.list()]
        self.db.execute("INSERT OR REPLACE INTO projects(id,tenant,name,risk_tier,budget_usd,owners,template,skills,created)"
                        " VALUES(?,?,?,?,?,?,?,?,?)", (project, tenant, project, risk_tier, budget_usd, j(owners), template,
                                                      j(skills), now()))
        sha = self.repo.create(project, self.templates / template,
                               {"project": project, "tenant": tenant, "owners": owners, "risk_tier": risk_tier})
        self.ledger.record("project.onboarded", project, actor, {"tenant": tenant, "owners": owners, "risk_tier": risk_tier,
                                                                  "budget_usd": budget_usd, "template": template,
                                                                  "repo_head": sha}, project)
        return {"project": project, "repo_head": sha, "owners": owners}

    def project(self, pid: str) -> dict:
        r = self.db.one("SELECT * FROM projects WHERE id=?", (pid,))
        r["owners"], r["skills"] = unj(r["owners"]), unj(r["skills"])
        return r
