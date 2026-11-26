# requirements-refinement

Refine business intent into testable requirements in the normalized spec contract.

## Inputs
- opportunity brief or draft spec

## Outputs
- gap list
- clarification questions
- EARS-style requirement drafts

## Procedure
1. Check every requirement maps to a business outcome id (BO-n).
2. Rewrite each requirement as one SHALL statement with a single observable behaviour.
3. For each requirement ensure at least one acceptance criterion with GIVEN/WHEN/THEN and a machine-checkable request/expect block.
4. Flag non-measurable words (fast, appropriate, robust) and ask for thresholds.
5. When two criteria conflict, raise a review item; never choose one silently.

## Limitations
- Cannot accept requirements on behalf of the product owner.
- Does not infer data classification.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
