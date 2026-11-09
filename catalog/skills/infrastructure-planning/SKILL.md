# infrastructure-planning

Plan infrastructure-as-code changes (Terraform on Azure) that pass policy checks.

## Inputs
- service contract
- NFRs

## Outputs
- IaC change plan
- policy pre-check results

## Procedure
1. Use workload identities and Key Vault references; never inline secrets.
2. public_network_access_enabled must be false for data services.
3. Pin provider versions exactly.

## Limitations
- Plans only; scoped deployment identity applies changes via the promotion pipeline.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
