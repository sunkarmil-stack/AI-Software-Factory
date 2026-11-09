# AI Software Factory — reference implementation (MVP thin vertical slice)

A governed, spec-driven software factory that turns **approved business intent** into
**specified, implemented, tested and deployable** software using scoped agents and versioned
skills. It implements the MVP baseline in *AI_Software_Factory_Project_Requirements v1.0
(2026-11-06)*.

```
approved spec ──► Pipeline A (validate · analyse · threat-model · plan · PO+architect baseline)
             ──► Stage 3 agents implement in an isolated sandbox ──► pull request
             ──► engineer review (bound to head SHA) ──► Pipeline B trusted CI on exact revision
             ──► merge ──► CI on merge commit ──► signed artifact + SLSA-style provenance + SBOM
             ──► release manifest ──► release-advisor summary ──► release-owner approval (bound)
             ──► Pipeline D promotion: canary → partial → full in nonprod, auto-rollback
             ──► audit reconstruction from the deployed digest
```

## Quick start

```bash
python -m factory.cli demo            # full slice end to end (~3 s, offline)
python -m factory.cli demo-rag        # bounded RAG agent + evaluation gate (ACC 07)
python -m pytest                      # 52 tests incl. acceptance scenarios ACC 01-09
python -m factory.cli serve           # control-plane API on :8088, ops dashboard at /dashboard
python -m factory.cli policy-test     # executable policy suite
python -m factory.cli audit REL-...   # auditor reconstruction of a release
```

State goes to `$FACTORY_HOME` (default `./.factory-home`). Requirements: Python ≥ 3.11 with
`pydantic`, `pyyaml`, `jsonschema`, `cryptography`, `starlette`, `uvicorn`, `pytest`; `git` on PATH.

## What is in the box

| Layer (Sec. 4) | Module | What it does |
|---|---|---|
| Experience | `factory/api.py`, `factory/cli.py`, `factory/ops/dashboard.py` | REST API (durable run ids, 429 admission), CLI, 3-plane ops dashboard |
| Control plane | `core/policy.py`, `core/approvals.py`, `specs/`, `skills/registry.py`, `core/evidence.py` | Executable policy + autonomy tiers, bound approvals, spec registry + trace graph, signed skill registry, hash-chained evidence ledger |
| Delivery execution | `orchestrator/engine.py`, `agents/`, `delivery/sandbox.py`, `delivery/repo.py` | Durable workflow engine (leases, checkpoints, limits), 9 agent roles on one worker type, sandboxes, git with server-side branch protection |
| Trusted build/release | `delivery/ci.py`, `delivery/release.py` | Pipeline B checks, QA-protected acceptance suite, reproducible build, signed provenance; Pipeline D promotion, staged rollout, rollback, drift |
| Product runtime | `product/rag_app.py`, `evals/harness.py` | Bounded RAG agent with read-only tools + human review queue; Pipeline C evaluation gate |
| Shared services | `models/gateway.py`, `tools/gateway.py`, `tools/mcp.py`, `context/` | Model gateway (approved routing, quotas, budgets, fallbacks), tool gateway (authz, idempotency, breakers, DLQ, kill switch), MCP adapter, retrieval + memory |

Catalog (`catalog/`): 9 agent manifests, 9 versioned prompts (+1 candidate), **11 skill packages**,
9 tool contracts, identities, model deployments, policy + policy tests, build standards.
Template (`templates/fastapi-service/`): hexagonal FastAPI service, k8s manifests, Terraform, lockfile.

## Honest scope notes

- **Offline model provider by default.** Agents run against a deterministic provider so the whole
  governed pipeline is runnable and testable with no model access. Anthropic and Azure OpenAI
  providers are wired through the model gateway (`catalog/models.yaml`, credential via env/secret ref)
  but were not exercised here. Two offline handlers deliberately *follow injected instructions* so the
  tests prove containment comes from identity/policy/tool gateway, not model refusal.
- **Sandbox isolation:** the default `process` backend (scrubbed env, rlimits, blackholed proxy) is
  dev-only; a `container` backend (`--network none --read-only --cap-drop ALL`) is used when
  `FACTORY_SANDBOX=container` and Docker is available. Hostile-code isolation (gVisor/Kata) is a Phase 0 spike.
- **FastAPI:** generated services keep business logic framework-neutral; the FastAPI adapter
  (`app/main.py`) is generated but FastAPI was not installed in the build environment, so tests and
  smoke checks run through the neutral dispatcher.
- **Stand-ins:** SQLite (→ PostgreSQL), on-disk Ed25519 keys (→ Key Vault/HSM), YAML policy engine
  (→ OPA/Cedar decision pending), local git (→ enterprise repo provider), local registry/environments
  (→ OCI registry + GitOps on AKS), dev bearer tokens (→ Entra ID). Retrieval is BM25 + TF-IDF fused
  (no dense embeddings).
- **Not implemented:** REQ 034, REQ 057, gRPC adapter, Spec Kit adapter (OpenSpec chosen), retention
  schedules (NFR 11), DR targets (NFR 05 regional). See `docs/traceability.md` for per-requirement status.

## Plugging in a real model

Set `ANTHROPIC_API_KEY`, then point an agent at the approved deployment, e.g. in
`catalog/agents/planning.yaml`: `model.deployment: claude-delivery`. The gateway enforces the
deployment's approved uses and data classes, falls back only to explicitly approved deployments,
and every output still has to pass the agent's schema and the same downstream gates.

## Docs

- `docs/architecture.md` — layers, boundaries A–D, gateways, pipelines A–E, autonomy tiers
- `docs/traceability.md` — REQ/NFR/ACC → implementation → test, with status
- `docs/decisions.md` — inception decisions recorded as ADRs (defaults + revisit triggers)
- `docs/golden-path.md` — onboarding guide for a pilot team (ACC 10)
- `docs/runbooks.md` — kill switch, stuck run, DLQ replay, recovery exercise, evidence verification
