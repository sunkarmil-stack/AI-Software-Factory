# Requirements traceability

Status: **Done** = implemented and covered by an automated test here · **Partial** = implemented with a stated gap · **Expansion** = post-pilot item · **Not built**.
Tests: `TA` = tests/test_acceptance_scenarios.py, `TC` = tests/test_controls.py, `TO` = tests/test_api_and_onboarding.py.

| Req | Status | Implementation | Test(s) / gap |
|---|---|---|---|
| REQ 001 | Done | `specs/trace.py`, `ops/audit.py` | TA acc09, acc01 |
| REQ 002 | Done | `specs/schema.py`, `SpecRegistry.completeness` | TC req002 |
| REQ 003 | Done (OpenSpec) | `specs/openspec_adapter.py` | TC req003 — Spec Kit adapter not built |
| REQ 004 | Done | `SpecRegistry.baseline`, `Trace.supersede` | TA acc01 |
| REQ 005 | Done | contradiction review items; spec agent blocks on vague terms/open assumptions | TC req005 ×2 |
| REQ 006 | Partial | ADR fields required in schema; unresolved decisions reported | TC req006 — no task-level ADR gate |
| REQ 007 | Done | `orchestrator/engine.py`, `workflows.py` | demo, TC req009/011 |
| REQ 008 | Done | checkpoints + leases + `recover()` | TC req008, TA acc04 |
| REQ 009 | Done | run/root limits: elapsed, attempts, tokens, cost, tool calls, fan-out | TC req009 |
| REQ 010 | Done | `agents/runtime.py` envelope + payload schema + one bounded repair | TC req010 |
| REQ 011 | Done | single engine; cancel propagation; LangGraph adapter keeps factory as parent | TC req011 |
| REQ 012 | Expansion (scaffold) | `orchestrator/langgraph_adapter.py` | contract test skips without langgraph |
| REQ 013 | Done | skill packages: digest, owner, compatibility, capabilities, data classes, tests, approval | TA acc03 |
| REQ 014 | Done | grants come only from runtime identity | TC req014, req020 |
| REQ 015 | Done | bootstrap pins skill + prompt digests in agent bundle | TC req015, req045 |
| REQ 016 | Done | static scan (injection, undeclared network, uncatalogued capability), behaviour tests, security approval for medium/high risk | TC req016 |
| REQ 017 | Expansion — done | revoke + dependent bundles + affected runs | TC req017 |
| REQ 018 | Done | `validate_contract` | TC req018 |
| REQ 019 | Partial | OpenAPI generated; contract + breaking-change tests in CI | gRPC adapter not built; FastAPI adapter not executed here |
| REQ 020 | Partial | `tools/mcp.py`: approved identity, pinned versions, metadata untrusted | TC req020 — real HTTP/stdio transport auth not built |
| REQ 021 | Done | gateway tenant/resource/grant/policy at call time | TC req021 |
| REQ 022 | Done | op ids, payload binding, reconcile against system of record | TA acc04, req022 |
| REQ 023 | Done | bulkhead, breaker, retry classes, DLQ, operator replay with re-authorization | TC req023, TA acc08 |
| REQ 024 | Done | ACL filter before scoring; model-request capture | TA acc06 |
| REQ 025 | Done | versions, provenance, revocation/deletion generation, fail-closed | TA acc06 |
| REQ 026 | Partial | heading-aware chunking, BM25+TF-IDF RRF, citations, separate retrieval/answer evals | TA acc07 — no dense embeddings |
| REQ 027 | Done | transient / checkpoint / persistent (quarantined) | TC req027 |
| REQ 028 | Expansion — partial | provenance + validation on promotion | TC req027 |
| REQ 029 | Partial | gateway approval/use/data-class + egress allowlist | TC req029 — network-layer block is a deployment control |
| REQ 030 | Done | prompt id/version/digest, deployment revision, settings, limitations in evidence | TC req030 |
| REQ 031 | Done | Pipeline C critical gates | TA acc07 |
| REQ 032 | Done | usage by tenant/project/run/root; budget stop; children cannot bypass | TC req032, req009 |
| REQ 033 | Done | explicit fallback chain with re-authorization | TC req033, TA acc08 |
| REQ 034 | Expansion — not built | | |
| REQ 035 | Done | QA store bound to QA approval; protected-path diff check | TC req035 |
| REQ 036 | Done | SAST authz rule + acceptance tests + secret/dependency/IaC checks | TC req036 |
| REQ 037 | Partial | categories: task success, groundedness, access, harmful actions, approval, termination | TA acc07 — reviewer adjudication queue not built |
| REQ 038 | Done | containment with an instruction-following model emulation | TA acc02, acc07 |
| REQ 039 | Partial | restart, timeout, duplicates, downstream failure, stale approval, 5× spike | TA acc04/05/08, TC req039 — spike run at reduced scale; no workload-profile report |
| REQ 040 | Expansion — partial | ops-triage agent proposes only | CLI `triage` |
| REQ 041 | Done | CI from exact SHA; agents cannot sign; merge bound to reviewed head | TC req041, TA acc02 |
| REQ 042 | Done | signed in-toto/SLSA provenance verified on promotion (no SLSA level claimed) | TC req042, TA acc09 |
| REQ 043 | Done | IaC policy, drift detection | TC req043 ×2 |
| REQ 044 | Partial | k8s security context checks; sandbox backends | process backend is dev-only; breakout tests need the isolation spike |
| REQ 045 | Done | digests, signed manifest, pinned prompt digest | TC req045, req042 |
| REQ 046 | Partial | staged rollout, pause/rollback | TC req046 — agent-state/data recovery not exercised |
| REQ 047 | Done | YAML policy + allow/deny tests + required controls | TC req047, CLI policy-test |
| REQ 048 | Done | binding (identity, role, action, digest, env, scope, state, expiry), re-check at use | TA acc05 ×2 |
| REQ 049 | Done | approver ≠ author; governance actions tier 3 + independent review | TC req049, TA acc05 |
| REQ 050 | Done | ledger links actor, run, spec, tools, approvals, artifacts | TA acc09 |
| REQ 051 | Done | redaction of secrets/PII in evidence and logs | TC req051 |
| REQ 052 | Done | scoped identities, scrubbed sandbox env, role revocation | TA acc02, acc05 |
| REQ 053 | Done | cancel run, revoke tool, narrow model, disable capability; fail-closed | TC req053 |
| REQ 054 | Done | 3-plane dashboard, failure categories | TO api test |
| REQ 055 | Partial | `docs/runbooks.md` | recovery exercise by another team is a human activity |
| REQ 056 | Done | onboarding: owners, risk tier, skills, budget, repo template | TO onboarding ×2 |
| REQ 057 | Expansion — not built | usage table supports showback later | |
| REQ 058 | Partial | cycle time, rework proxy, cost; generated code volume excluded | escaped defects/incidents/adoption not yet measured |

## NFRs

| NFR | Status | Evidence |
|---|---|---|
| 01 Availability | Not measured | deployment concern |
| 02 API latency / durable id | Done | TO api test (< 1 s, durable `RUN-` id) |
| 03 Reference scale | Partial | TA acc08 runs 60 runs at 4 workers; full 20-project profile pending |
| 04 Spike behaviour | Partial | explicit rejection, no lost accepted work, zero duplicate writes (TA acc04/req022); backlog-recovery timing not measured |
| 05 Recovery | Partial | zero loss on worker restart tested; regional RPO/RTO not built |
| 06 Kill control | Done | TC req053 (refresh 5 s ≪ 60 s target; fail-closed past max age) |
| 07 Evidence | Done | TC nfr07, TA acc09 |
| 08 Isolation | Done for defined tests | TA acc06, TC req021 |
| 09 Quality gates | Done | TA acc07 |
| 10 Cost | Done | TC req032 |
| 11 Retention | Not built | retention fields only |

## Acceptance scenarios

ACC 01–09: `tests/test_acceptance_scenarios.py` (all passing). ACC 10: automated portion in `TO test_second_team_golden_path`; the human exercise is described in `docs/golden-path.md`.

## Business ontology, rules and actions (requirements v1.1)

`TO2` = tests/test_ontology.py, `TR` = tests/test_ontology_rules_actions.py.

| Req | Status | Implementation | Test(s) / gap |
|---|---|---|---|
| REQ 059 | Done | `ontology/registry.py`: versioned concepts, digests, history | TO2 seed, tiered approval |
| REQ 060 | Done | proposals only; nothing applies before approval | TO2 discovery, tiered approval |
| REQ 061 | Done | `ontology/discovery.py`: contract extraction + ontology agent with evidence | TO2 discovery; TR discovery |
| REQ 062 | Done | registry validation | TO2 validation, ambiguous synonym |
| REQ 063 | Done | tiering, bound approvals, separation of duties | TO2 tiered approval, restricted, tamper; TR discovery roles |
| REQ 064 | Done | `ontology/rules.py` safe evaluator | TR rule engine, validation |
| REQ 065 | Done | action contract validation | TR validation, consequential needs security |
| REQ 066 | Done | `ontology/actions.py` execution | TR execute, block, require approval, who may run |
| REQ 067 | Done | `ontology-actions` workload via tool gateway, stable op id | TR execute, require approval (no duplicate) |
| REQ 068 | Done | CONCEPT trace nodes, breaking vs additive | TO2 breaking change; TR rule change invalidates release |
| REQ 069 | Done | `Ontology.check_spec` in Pipeline A | TO2 spec check; TR contract vs rules |
| REQ 070 | Partial | Ontology Studio, `/v1/ontology/*`, CLI | TO2 API/UI — dev sign-in only; Entra ID not integrated |
| REQ 071 | Expansion — not built | | FIBO alignment, cross-object rules, graph retrieval, graph store |
| ACC 11 | Done | | TO2 discovery (embedded instruction has no effect) |
| ACC 12 | Done | | TR block rule, require-approval request |
| ACC 13 | Done | | TR rule change invalidates release; TO2 breaking change |
