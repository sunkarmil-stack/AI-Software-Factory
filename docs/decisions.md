# Architecture decisions (Section 15)

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

## Production decisions (requirements v2.0)

ADR-F04, F05, F06 and F09 are superseded for production by F13, F12, F14 and F15 respectively.

| ADR | Decision | Options considered | Consequences | Revisit trigger |
|---|---|---|---|---|
| ADR-F11 Enterprise AI gateway | Azure API Management (internal VNet, GenAI policies) is the **single egress** for model, MCP and SoR traffic; factory model/tool gateways stay as the inner layer | Direct provider SDKs from the factory; LiteLLM/Portkey proxy; Kong AI gateway | Enterprise owns quotas, content safety, revocation and upstream credentials; factory keeps business authorization and idempotency; two-layer kill switch; extra hop (NFR 12) | Enterprise standardizes on another gateway product — only `AIGatewayClient` and the APIM policies change |
| ADR-F12 Keys and secrets | Key Vault Premium, EC-HSM P-256, one key per signing identity, per-key RBAC, yearly auto-rotation | Managed HSM; Sigstore keyless; Notation | Private keys never leave the HSM; signing latency per call; old signatures verifiable via key versions | Signing volume exceeds Key Vault throughput → Managed HSM |
| ADR-F13 State store | PostgreSQL Flexible Server 16, zone-redundant HA, geo backup, Entra auth only; same schema as SQLite via `translate()`; evidence append-only by trigger | Cosmos DB; Azure SQL | One schema for dev and prod; DB-enforced immutability; advisory lock serializes ledger appends | Ledger append contention under load test |
| ADR-F14 Sandbox | Kubernetes Jobs on an AKS Kata (`KataVmIsolation`) node pool, deny-all egress, restricted PSS | gVisor; Azure Container Instances confidential; Firecracker | VM-level isolation for untrusted generated code without operating a separate fleet | Kata pool capacity or cold-start time blocks CI targets |
| ADR-F15 IaC and deployment | Terraform (azurerm pinned) for Azure; Helm chart; GitOps reconciliation (Argo CD/Flux) from a protected environments repo | Bicep; kubectl apply from CI | No CI credential can change production directly; desired state is auditable in git | Platform team mandates Bicep or a different reconciler |
| ADR-F16 Identity | Entra ID for humans (app roles, group→tenant) and workloads (federated credentials, separate API/worker/migrate identities) | Shared service principal with secret | No stored secrets; least privilege per component | — |
| ADR-F17 Promotion adapter | Pluggable `DeploymentTarget`; GitOps implemented; Harness/Helios defined as an adapter that refuses until built. Prod requires release owner + change manager bound to a change record, nonprod first | Hard-wire Harness/Helios now | Factory approval binding and evidence stay identical across targets | Harness/Helios integration funded (REQ 088) |
