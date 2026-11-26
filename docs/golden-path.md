# Golden path for a pilot team (ACC 10)

1. **Onboard** (platform admin): `Factory.onboard(project, tenant, owners, risk_tier, budget_usd)` or `POST /v1/projects`. Owners must include product_owner, architect, engineer, qa_owner, release_owner and each must hold that role. A repository is created from `templates/fastapi-service`.
2. **Write the spec** (product owner): copy `samples/specs/accounts-api.v1.yaml`. Every requirement needs an outcome id and ≥ 1 acceptance criterion with a machine-checkable `check.request` / `check.expect`.
3. **Submit** (product owner): `POST /v1/runs {"workflow":"spec_pipeline","project":..,"input":{"spec":..}}`. Fix anything reported as missing, contradictory or vague.
4. **Baseline**: product owner (and architect for tier ≥ 2) approve via `POST /v1/runs/{id}/approvals {"step_id":"approve_baseline","role":..}`.
5. **Publish acceptance tests** (QA owner): `f.ci.qa.materialize(spec, qa_owner)` — the protected suite CI will use.
6. **Deliver**: start `delivery` with `{"spec_id","version"}`. Agents implement in a sandbox and open a PR. Read the advisory review findings.
7. **Release**: start `release` with `{"spec_id","version","pr_id","env":"nonprod"}`; an engineer approves the exact head SHA; CI, merge and packaging run; a release owner (not the author) approves the manifest for the environment.
8. **Verify**: `python -m factory.cli audit <REL-id>` and check the dashboard.

Record for ACC 10: setup time, every place you needed help, missing docs, and operating gaps.
