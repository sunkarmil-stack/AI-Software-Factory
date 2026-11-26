# api-contract-design

Design versioned OpenAPI contracts for FastAPI services, with explicit scopes and error codes.

## Inputs
- requirements
- data classification

## Outputs
- contract endpoints with operation ids, scopes, responses

## Procedure
1. One operation id per endpoint; nouns for resources.
2. Every endpoint declares required_scope and error responses (401/403/404).
3. Breaking changes require a major contract version bump and consumer contract tests.

## Limitations
- gRPC contracts are produced only where an ADR selects gRPC.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
