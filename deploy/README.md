# Deployment (Azure production)

> **Status:** authored against documented Azure/APIM/AKS/Helm APIs. **Not** applied, `terraform validate`d, `helm lint`ed or image-built in the reference build environment (no network, no terraform/helm/docker daemon). Treat as a reviewed starting point; first apply happens in the dev subscription (see `docs/production.md` §7).

| Path | What it creates |
|---|---|
| `terraform/` | RG, VNet + subnets + private DNS, private AKS (Entra-only, workload identity, Cilium network policy, **Kata sandbox pool**), ACR (private), PostgreSQL Flexible Server 16 (private, zone-redundant HA, geo backup, **Entra-only auth**), Key Vault Premium (RBAC, purge protection, **EC-HSM P-256 signing keys** with per-key role grants), WORM evidence storage, Log Analytics + App Insights, user-assigned identities + federated credentials, **enterprise AI gateway (APIM, internal VNet)** or registration on a shared one |
| `apim/` | Gateway policies: Entra token validation, enterprise kill switch, per-asset revocation, `llm-token-limit`, content safety, token metrics, managed-identity auth to upstreams, idempotency-key enforcement on SoR writes |
| `helm/ai-software-factory/` | API + worker Deployments (separate identities), pre-upgrade migration Job, service accounts with workload-identity annotations, default-deny NetworkPolicies, PDB/HPA, sandbox namespace (restricted PSS, deny-all egress, quotas) |
| `../Dockerfile` | Non-root image; built only by trusted CI, deployed by digest |
| `../migrations/postgres/` | `bootstrap/000_roles.sql` (once, manual), `001`–`003` applied by the migration Job |

## Order of operations
1. `terraform init -backend-config=env/<env>.backend.hcl && terraform apply -var-file=env/<env>.tfvars`
2. Run `migrations/postgres/bootstrap/000_roles.sql` once as the migrate identity.
3. CI builds and signs the image, pushes to ACR, records the digest.
4. Fill `values-<env>.yaml` from `terraform output` (client IDs, KV URL, gateway URL, Postgres FQDN, subnet CIDRs).
5. Argo CD/Flux syncs the chart from the protected environments repo; the migration hook runs first.
6. `kubectl exec deploy/factory-api -- python -m factory.cli preflight` must report all PASS, **no exemptions**.
