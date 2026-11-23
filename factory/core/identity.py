"""Principals, roles and workload identities (Section 3, REQ 014, REQ 052).

Agents never inherit human roles because a prompt names a role: an agent principal's
`kind` is always "agent" and approval checks require kind == "human".
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

import yaml

HUMAN_ROLES = {"product_owner", "architect", "engineer", "qa_owner", "security", "model_owner",
               "data_owner", "release_owner", "operator", "auditor", "platform_admin", "ontology_owner", "supervisor"}


@dataclass
class Principal:
    id: str
    kind: str  # human | agent | workload
    roles: set[str] = field(default_factory=set)
    tenants: set[str] = field(default_factory=set)
    grants: list[str] = field(default_factory=list)  # "tool:action@resource-pattern"
    token: str | None = None

    def has_role(self, role: str) -> bool:
        return self.kind == "human" and role in self.roles

    def can(self, tool: str, action: str, resource: str) -> bool:
        for g in self.grants:
            ta, _, res = g.partition("@")
            if fnmatch.fnmatchcase(f"{tool}:{action}", ta) and fnmatch.fnmatchcase(resource, res or "*"):
                return True
        return False

    def in_tenant(self, tenant: str) -> bool:
        return "*" in self.tenants or tenant in self.tenants


class Directory:
    """Stand-in for Entra ID (humans) + workload identity federation (agents/services)."""

    def __init__(self):
        self.principals: dict[str, Principal] = {}

    def add(self, p: Principal) -> Principal:
        if p.kind == "agent" and p.roles & HUMAN_ROLES:
            raise ValueError(f"agent {p.id} cannot be assigned human roles {p.roles & HUMAN_ROLES}")
        self.principals[p.id] = p
        return p

    def get(self, pid: str) -> Principal:
        if pid not in self.principals:
            raise KeyError(f"unknown principal {pid}")
        return self.principals[pid]

    def by_token(self, token: str) -> Principal | None:
        for p in self.principals.values():
            if p.token and p.token == token:
                return p
        return None

    def revoke(self, pid: str) -> None:
        p = self.principals.get(pid)
        if p:
            p.grants = []
            p.token = None

    @classmethod
    def load(cls, path: Path) -> "Directory":
        d = cls()
        for row in yaml.safe_load(Path(path).read_text())["principals"]:
            d.add(Principal(id=row["id"], kind=row["kind"], roles=set(row.get("roles", [])),
                            tenants=set(row.get("tenants", [])), grants=list(row.get("grants", [])),
                            token=row.get("token")))
        return d
