CREATE INDEX IF NOT EXISTS idx_runs_state_lease ON runs(state, lease_until);
CREATE INDEX IF NOT EXISTS idx_evidence_subject ON evidence(subject);
CREATE INDEX IF NOT EXISTS idx_evidence_project_kind ON evidence(project, kind);
CREATE INDEX IF NOT EXISTS idx_trace_edges_dst ON trace_edges(dst);
CREATE INDEX IF NOT EXISTS idx_deployments_project_env ON deployments(project, env, status);
CREATE INDEX IF NOT EXISTS idx_onto_proposals_status ON onto_proposals(status);
CREATE INDEX IF NOT EXISTS idx_action_requests_status ON action_requests(status);
