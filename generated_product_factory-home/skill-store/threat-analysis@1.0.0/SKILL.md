# threat-analysis

Produce STRIDE threat scenarios per endpoint and agent tool, with mitigations and test hooks.

## Inputs
- contracts
- tool contracts

## Outputs
- threat list

## Procedure
1. Enumerate spoofing, tampering, repudiation, information disclosure, denial of service and elevation per entry point.
2. For agent tools, include prompt-injection and data-leakage scenarios with an enforcement point outside the model.
3. Link each mitigation to a test or control id.

## Limitations
- Advisory; security owns the threat model.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
