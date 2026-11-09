# incident-triage

Triage sanitized telemetry into incident or fix proposals with explicit uncertainty.

## Inputs
- sanitized metrics
- recent deployments

## Outputs
- incident proposals

## Procedure
1. Separate factory, delivery and product planes.
2. Classify: queue backlog, provider throttling, validation failure, application error, business exception.
3. State confidence and what evidence would confirm the hypothesis.
4. Create reviewed backlog items; never modify telemetry or production.

## Limitations
- Correlation is not causation; proposals require human review.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
