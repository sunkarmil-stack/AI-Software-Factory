# release-evidence-assembly

Assemble and summarize release evidence for human release owners.

## Inputs
- release manifest
- check reports

## Outputs
- evidence completeness summary

## Procedure
1. List each required check and its status for the exact artifact digest.
2. Call out stale approvals or superseded requirements.
3. Recommend; never authorize.

## Limitations
- Cannot authorize promotion.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
