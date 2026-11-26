# test-design

Propose unit, contract and risk-targeted test candidates from acceptance criteria.

## Inputs
- acceptance criteria

## Outputs
- test candidate files

## Procedure
1. Generate at least one test per acceptance criterion, named after the AC id.
2. Add negative tests for authorization, tenant isolation and validation.
3. Never modify tests/acceptance — those are QA-owned protected assets.

## Limitations
- Candidates do not determine readiness; protected acceptance tests do.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
