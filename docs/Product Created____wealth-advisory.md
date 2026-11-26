# Wealth advisory product (domain pack `wealth-advisory`)

The first product built on the factory beyond the accounts sample. It shows what a new line of business
configures, adds and needs from the platform.

```bash
python -m factory.cli domain show wealth-advisory   # the pack
python -m factory.cli domain load wealth-advisory   # onboard + propose ontology (owners must approve)
python -m factory.cli demo-wealth                   # end to end, offline, synthetic book
python -m pytest tests/test_wealth_advisory.py      # 18 tests
```

## 1. Configured (no code)

| Item | Where |
|---|---|
| Project `wealth-advisory`, tenant `wealth`, risk tier 3, owners, budget | `catalog/domains/wealth-advisory/project.yaml` |
| Roles `advisor`, `portfolio_manager`, `compliance_officer`; people `po.wendy`, `adv.ada`, `adv.ben`, `pm.paolo`, `sup.sid`, `cco.cora`, `ops.wes` | `factory/core/identity.py`, `catalog/identities.yaml`; Entra app roles `Factory.Advisor`, `Factory.PortfolioManager`, `Factory.ComplianceOfficer`; group `grp-wealth` → tenant `wealth` |
| Delivery agents granted the `wealth` tenant | `catalog/identities.yaml` |
| Model route `advisory-copilot` (uses `product.advisory`, `product.suitability`; up to `confidential`; **restricted/MNPI not approved**) | `catalog/models.yaml`, `catalog/models.production.yaml` |
| Policy v1.2.0: `orders.submit` needs the advisor's bound confirmation | `catalog/policy/factory-core.yaml` (+2 policy tests) |

## 2. Added content

| Content | Files |
|---|---|
| Ontology: `InvestmentAccount`, `InvestmentProduct`, `Recommendation`, `TradeOrder`; 10 rules; 2 actions | `catalog/domains/wealth-advisory/ontology.yaml` (imported as governed proposals) |
| Tool contracts via the enterprise AI gateway: `portfolio`, `market_data`, `kyc`, `product_shelf` (read); `advice_records` (write, idempotent); `orders` (consequential, irreversible, no retry) | `catalog/tools/*.yaml` |
| Product agents `advisor-copilot`, `suitability-reviewer` + prompts + skill `suitability-assessment` (medium risk, security-approved at publish) | `catalog/agents`, `catalog/prompts`, `catalog/skills` |
| Evaluation suite: critical `grounding`, `no_forecasts`, `suitability_routing`, `kyc_block` | `catalog/domains/wealth-advisory/evals.yaml` |
| Spec `SPEC-WEALTH-ADVICE-API` (risk tier 3, step-up auth on writes) delivered through Pipelines A, B, D | `samples/specs/wealth-advice-api.v1.yaml` |
| Synthetic book of record (2 accounts, 6 products) | `samples/wealth/book.yaml` |

### Suitability rules

| Rule | Severity | Routed to |
|---|---|---|
| KYC older than 365 days (firm policy; confirm with Compliance) | block | |
| Product not know-your-product reviewed | block | |
| Rationale < 40 characters; side not buy/sell | block | |
| Proprietary product, conflict not disclosed | block | |
| Product risk > client risk tolerance | require approval | supervisor |
| Post-trade weight > IPS concentration limit | require approval | portfolio_manager |
| Proprietary product (conflict) | require approval | compliance_officer |
| Order without recommendation / non-positive amount | block | |

## 3. Code added

| Code | Why |
|---|---|
| `factory/product/wealth.py` | **Deterministic calculation service** (valuation, weights, drift, rebalance amounts, post-trade concentration, KYC age, snapshot id); advisor workflow; numeric grounding and forecast-language checks; product evaluation |
| `factory/domains.py` + CLI `domain` | Generic domain-pack loader: onboarding + ontology as proposals. The next line of business is configuration only |
| `factory/agents/offline.py` | Offline behaviour for the two product agents |
| `factory/services.py`, `core/db.py` | Synthetic systems of record (`advice_records_sor`, `orders_sor`); migration regenerated |

## 4. Platform changes this domain forced

1. **Tier-3 approval roles now come from policy.** The tool gateway hard-coded `release_owner` for every tier-3 call; it now reads `approval_roles` from policy obligations (default unchanged).
2. **Ontology actions can carry a human's bound confirmation into the tool call** (`ActionService.tool_binding`, `execute(..., tool_approvals=)`). An irreversible order needs the advisor's approval of the exact order payload.
3. **Gateway tool routes support path parameters and GET query strings** (`/accounts/{account_id}/holdings`).

## 5. Control design

- **Numbers come from code, not the model.** `advisor-copilot` receives the calculation snapshot. Any number in its notes that the snapshot can't justify, any proposal amount not produced by the calculation service, or any forecast/promise language sends the review to `needs_review`.
- **Facts are recomputed at the moment of recommendation** from systems of record. Model-proposed facts are never used.
- **Approvals bind to the exact parameters.** Changing the amount after a supervisor approved gets `pending_approval` again.
- **Orders implement recommendations.** Product, side and amount come from the recorded recommendation. Only the advisor of record can place it, with an explicit confirmation. One order per recommendation is enforced in the app and again at the system of record.
- **Agents can draft but can't act.** Neither product agent has an action or order grant.

## 6. Not built yet (blocks client-facing production)

| Gap | Note |
|---|---|
| Cross-object rules (REQ 071) | Concentration and household rules use facts computed by the calculation service. Native aggregate rules are not built |
| Records retention (NFR 11) | Advice records need the firm's books-and-records schedule and the WORM exporter |
| Information barriers | Needed before any `restricted` (MNPI) data class is approved for the model route |
| Client-facing disclosure gate | Disclosure documents and plain-language checks before anything reaches a client |
| Model inventory export (OSFI E-23, effective May 1, 2027) | Agent bundle + eval results as inventory records |
| Live stand-ins | The Azure stand-in does not emulate the six wealth APIs; production calls are untested |
| Ontology Studio SSO (MSAL) | Advisors and compliance staff need real sign-in |
