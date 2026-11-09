# Inception decisions (Section 15) — defaults taken in this reference implementation

Each is a planning default, not an approved enterprise decision. Owner and decision date to be filled during Phase 0.

| ADR | Decision (default) | Options considered | Consequences | Revisit trigger |
|---|---|---|---|---|
| ADR-F01 Orchestration | Factory-native durable engine owns state; LangGraph adapter for the spike | LangGraph, CrewAI, AutoGen | Idempotency/approvals stay with the factory regardless of framework | Spike shows LangGraph interrupts + checkpoints pass the lifecycle contract with less code |
| ADR-F02 Spec framework | OpenSpec adapter over a factory-owned normalized spec | Spec Kit, OpenSpec | Enterprise fields kept in `factory.yaml` sidecar and flagged | Pilot teams prefer Spec Kit workflow |
| ADR-F03 Policy engine | YAML rules, deny-overrides, tested in CI | OPA/Rego, Cedar | Simple and auditable; limited expressiveness | Policy count or attribute complexity exceeds YAML readability |
| ADR-F04 State store | SQLite WAL (reference) → Azure PostgreSQL | Cosmos DB, PostgreSQL | Same schema; transactions for op-id claims | Load test shows contention |
| ADR-F05 Keys | Ed25519 per trusted identity → Key Vault/Managed HSM | Sigstore keyless, KV | Agents never hold keys | Enterprise adopts Sigstore/Notation |
| ADR-F06 Sandbox | process (dev) / container `--network none`; tier ≥ 2 needs assessed isolation | gVisor, Kata, Firecracker | Hostile-code isolation not claimed | Isolation spike result |
| ADR-F07 Retrieval | BM25 + TF-IDF RRF; ACL before scoring | Azure AI Search hybrid with embeddings | No embedding provider needed for the pilot | Recall@k below target on real corpus |
| ADR-F08 Model routing | Offline default; Anthropic / Azure OpenAI behind gateway; explicit fallbacks only | Direct SDKs | Same governance for every provider | Vendor data terms approved |
| ADR-F09 IaC | Terraform (azurerm pinned) | Bicep | One IaC standard for MVP | Platform team standard changes |
| ADR-F10 Approvals | Factory approval service with signed, bound records | ServiceNow / enterprise workflow | Must integrate with enterprise approval system before prod | Integration available |
