-- GENERATED from factory/core/db.py SCHEMA by factory.cli migrate --emit. Do not edit by hand.
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, tenant TEXT, name TEXT, risk_tier INTEGER,
  budget_usd DOUBLE PRECISION, owners TEXT, template TEXT, skills TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS specs(spec_id TEXT, version INTEGER, digest TEXT, body TEXT, status TEXT,
  project TEXT, created DOUBLE PRECISION, created_by TEXT, PRIMARY KEY(spec_id, version));
CREATE TABLE IF NOT EXISTS trace_nodes(id TEXT PRIMARY KEY, kind TEXT, ref TEXT, digest TEXT, status TEXT,
  project TEXT, data TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS trace_edges(src TEXT, dst TEXT, rel TEXT, PRIMARY KEY(src, dst, rel));
CREATE TABLE IF NOT EXISTS skills(name TEXT, version TEXT, digest TEXT, signature TEXT, manifest TEXT,
  path TEXT, status TEXT, owner TEXT, published DOUBLE PRECISION, PRIMARY KEY(name, version));
CREATE TABLE IF NOT EXISTS tool_contracts(name TEXT, version TEXT, digest TEXT, contract TEXT, status TEXT,
  PRIMARY KEY(name, version));
CREATE TABLE IF NOT EXISTS agents(id TEXT, version TEXT, digest TEXT, manifest TEXT, status TEXT,
  PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, parent_id TEXT, root_id TEXT, workflow TEXT,
  project TEXT, tenant TEXT, actor TEXT, state TEXT, input TEXT, limits TEXT, counters TEXT,
  created DOUBLE PRECISION, updated DOUBLE PRECISION, error TEXT, lease_owner TEXT, lease_until DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS steps(run_id TEXT, step_id TEXT, state TEXT, attempts INTEGER, output TEXT,
  updated DOUBLE PRECISION, PRIMARY KEY(run_id, step_id));
CREATE TABLE IF NOT EXISTS operations(op_id TEXT PRIMARY KEY, tool TEXT, caller TEXT, payload_hash TEXT,
  state TEXT, result TEXT, created DOUBLE PRECISION, updated DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS dlq(id TEXT PRIMARY KEY, tool TEXT, caller TEXT, tenant TEXT, payload TEXT,
  error TEXT, approval_id TEXT, created DOUBLE PRECISION, replayed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, approver TEXT, role TEXT, action TEXT,
  binding TEXT, binding_digest TEXT, expires DOUBLE PRECISION, signature TEXT, created DOUBLE PRECISION, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS review_items(id TEXT PRIMARY KEY, project TEXT, kind TEXT, ref TEXT,
  summary TEXT, status TEXT, data TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS evidence(seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, id TEXT UNIQUE, kind TEXT,
  subject TEXT, actor TEXT, project TEXT, data TEXT, digest TEXT, prev_hash TEXT, hash TEXT, ts DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS controls(id TEXT PRIMARY KEY, kind TEXT, target TEXT, actor TEXT, reason TEXT,
  active INTEGER, ts DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY, root_run TEXT, run TEXT, project TEXT, tenant TEXT,
  kind TEXT, units DOUBLE PRECISION, cost DOUBLE PRECISION, latency_ms DOUBLE PRECISION, ts DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS prs(id TEXT PRIMARY KEY, project TEXT, branch TEXT, head_sha TEXT, base TEXT,
  author TEXT, status TEXT, title TEXT, created DOUBLE PRECISION, merged_sha TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS builds(id TEXT PRIMARY KEY, project TEXT, source_sha TEXT, artifact_digest TEXT,
  provenance TEXT, status TEXT, report TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS releases(id TEXT PRIMARY KEY, project TEXT, manifest TEXT, manifest_digest TEXT,
  signature TEXT, status TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS deployments(id TEXT PRIMARY KEY, release_id TEXT, project TEXT, env TEXT,
  artifact_digest TEXT, status TEXT, data TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS metrics(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, plane TEXT, name TEXT,
  labels TEXT, value DOUBLE PRECISION, ts DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS memory(id TEXT PRIMARY KEY, kind TEXT, scope TEXT, owner TEXT, project TEXT,
  tenant TEXT, data TEXT, provenance TEXT, status TEXT, retention_until DOUBLE PRECISION, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS docs(id TEXT, version INTEGER, tenant TEXT, project TEXT, acl TEXT,
  classification TEXT, title TEXT, text TEXT, source TEXT, deleted INTEGER DEFAULT 0, updated DOUBLE PRECISION,
  PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS backlog(id TEXT PRIMARY KEY, project TEXT, source TEXT, title TEXT,
  detail TEXT, status TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS onto_concepts(name TEXT, version INTEGER, body TEXT, digest TEXT, semantic_digest TEXT,
  status TEXT, created DOUBLE PRECISION, created_by TEXT, proposal TEXT, PRIMARY KEY(name, version));
CREATE TABLE IF NOT EXISTS onto_proposals(id TEXT PRIMARY KEY, kind TEXT, target TEXT, payload TEXT, payload_digest TEXT,
  provenance TEXT, proposer TEXT, proposer_kind TEXT, tier INTEGER, roles TEXT, status TEXT, validation TEXT,
  approvals TEXT, decided_by TEXT, reason TEXT, created DOUBLE PRECISION, decided DOUBLE PRECISION, target_version INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS action_requests(id TEXT PRIMARY KEY, action TEXT, actor TEXT, subject TEXT,
  params TEXT, env TEXT, tenant TEXT, status TEXT, roles TEXT, approvals TEXT, rule_results TEXT, result TEXT,
  created DOUBLE PRECISION, decided DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS holds_sor(hold_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, account_id TEXT,
  amount_cents INTEGER, reason TEXT, status TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS advice_records_sor(recommendation_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT,
  account_id TEXT, product_code TEXT, side TEXT, amount_cents INTEGER, rationale TEXT, snapshot_id TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS return_registry_sor(certification_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT,
  entity_id TEXT, return_code TEXT, period_end TEXT, package_digest TEXT, snapshot_id TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS filings_sor(filing_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, entity_id TEXT,
  certification_id TEXT, return_code TEXT, period_end TEXT, package_digest TEXT, receipt TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS risk_cases_sor(case_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, account_id TEXT,
  case_type TEXT, alert_id TEXT, score DOUBLE PRECISION, reason_codes TEXT, status TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS restrictions_sor(restriction_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, account_id TEXT,
  case_id TEXT, restriction TEXT, duration_hours INTEGER, status TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS limit_changes_sor(change_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, account_id TEXT,
  case_id TEXT, current_limit_cents INTEGER, new_limit_cents INTEGER, notice_days INTEGER, status TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS orders_sor(order_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, account_id TEXT,
  recommendation_id TEXT, product_code TEXT, side TEXT, amount_cents INTEGER, status TEXT, created DOUBLE PRECISION);
