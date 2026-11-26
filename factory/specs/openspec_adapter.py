"""OpenSpec adapter (REQ 003).

Exports the normalized spec to an OpenSpec-style tree:
  openspec/specs/<capability>/spec.md   (### Requirement / #### Scenario WHEN/THEN)
  openspec/factory.yaml                 (enterprise fields OpenSpec has no native slot for)
Import reverses it. Stable IDs travel in HTML comments; enterprise-only fields are
retained in the sidecar and reported as `unsupported_native` so nothing is silently lost.
Pin the adopted OpenSpec version in catalog/adapters.yaml when implementing against a release.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

NATIVE = {"requirements", "title"}


def export(spec: dict, root: Path) -> dict:
    root = Path(root)
    cap = re.sub(r"[^a-z0-9]+", "-", spec["title"].lower()).strip("-")
    d = root / "openspec" / "specs" / cap
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"# {spec['title']}", f"<!-- spec_id: {spec['spec_id']} version: {spec['version']} -->", "",
             "## Requirements", ""]
    for r in spec["requirements"]:
        lines += [f"### Requirement: {r.get('name', r['id'])}", f"<!-- id: {r['id']} outcome: {r['outcome']}"
                  f" decisions: {','.join(r.get('decisions', []))} -->", r["statement"], ""]
        for ac in r["acceptance_criteria"]:
            lines += [f"#### Scenario: {ac.get('name', ac['id'])}", f"<!-- id: {ac['id']} -->",
                      f"- **GIVEN** {ac['given']}", f"- **WHEN** {ac['when']}", f"- **THEN** {ac['then']}",
                      "", "```json factory-check", json.dumps(ac["check"], sort_keys=True), "```", ""]
    (d / "spec.md").write_text("\n".join(lines), encoding="utf-8")
    sidecar = {k: v for k, v in spec.items() if k not in NATIVE}
    (root / "openspec" / "factory.yaml").write_text(yaml.safe_dump(sidecar, sort_keys=True), encoding="utf-8")
    return {"capability": cap, "unsupported_native": sorted(sidecar.keys())}


def import_(root: Path) -> tuple[dict, dict]:
    root = Path(root) / "openspec"
    sidecar = yaml.safe_load((root / "factory.yaml").read_text(encoding="utf-8"))
    md_files = sorted((root / "specs").glob("*/spec.md"))
    if len(md_files) != 1:
        raise ValueError("adapter expects exactly one capability per normalized spec")
    text = md_files[0].read_text(encoding="utf-8")
    title = re.search(r"^# (.+)$", text, re.M).group(1)
    reqs, cur_r, cur_ac = [], None, None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("### Requirement: "):
            cur_r = {"name": ln[17:], "acceptance_criteria": []}
            m = re.match(r"<!-- id: (\S+) outcome: (\S+) decisions: ?(\S*) -->", lines[i + 1])
            cur_r["id"], cur_r["outcome"] = m.group(1), m.group(2)
            if m.group(3):
                cur_r["decisions"] = m.group(3).split(",")
            cur_r["statement"] = lines[i + 2]
            reqs.append(cur_r); i += 3; continue
        if ln.startswith("#### Scenario: "):
            cur_ac = {"name": ln[15:], "id": re.match(r"<!-- id: (\S+) -->", lines[i + 1]).group(1)}
            cur_r["acceptance_criteria"].append(cur_ac); i += 2; continue
        for kw in ("GIVEN", "WHEN", "THEN"):
            if ln.startswith(f"- **{kw}** "):
                cur_ac[kw.lower()] = ln[len(kw) + 7:]
        if ln.startswith("```json factory-check"):
            cur_ac["check"] = json.loads(lines[i + 1]); i += 3; continue
        i += 1
    for r in reqs:  # names defaulted to ids on export are not real data
        if r.get("name") == r["id"]:
            del r["name"]
        for ac in r["acceptance_criteria"]:
            if ac.get("name") == ac["id"]:
                del ac["name"]
    spec = {**sidecar, "title": title, "requirements": reqs}
    return spec, {"unsupported_native": sorted(sidecar.keys())}
