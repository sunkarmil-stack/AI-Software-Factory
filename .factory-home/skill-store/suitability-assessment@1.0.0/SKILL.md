# suitability-assessment

Assess a proposed recommendation against KYC, know-your-product, risk fit, concentration and conflicts, using only computed facts.

## Inputs
- calculation snapshot (holdings, prices, KYC, IPS, snapshot id)
- proposed recommendation (product, side, amount)

## Outputs
- findings, one per check
- draft suitability rationale in plain language for the advisor to edit

## Procedure
1. Confirm KYC is current against firm policy; if not, stop and say KYC must be updated first.
2. Confirm the product passed know-your-product review.
3. Compare product risk with the client's risk tolerance and explain any gap.
4. Compare post-trade concentration with the IPS limit.
5. Flag proprietary products as conflicts that need disclosure and compliance review.
6. Draft the rationale from the facts only: no forecasts, no promises, no numbers that are not in the snapshot.

## Limitations
- Findings are advisory; firm rules in the ontology and named approvers decide.
- Does not assess tax, estate or legal suitability.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
