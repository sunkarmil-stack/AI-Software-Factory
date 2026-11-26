# dependency-review

Review dependency changes for pinning, licence and known-vulnerability risk.

## Inputs
- requirements.lock diff

## Outputs
- dependency findings

## Procedure
1. Every dependency is pinned with ==.
2. New dependencies require an owner justification in the PR.
3. Flag packages not present in the enterprise mirror.

## Limitations
- Vulnerability data comes from the enterprise scanner; this skill does not query the Internet.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
