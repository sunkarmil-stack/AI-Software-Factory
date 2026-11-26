# fastapi-scaffolding

Generate a FastAPI service from the approved contract using the hexagonal template (framework-neutral handlers + FastAPI adapter).

## Inputs
- normalized spec
- contract

## Outputs
- app/routes.py
- app/main.py
- openapi.json

## Procedure
1. Generate one handler per endpoint; every non-public handler calls require_scope first.
2. Filter all data access by caller tenant; return 404 for foreign-tenant objects.
3. Validate request bodies against the contract schema; reject with 400.
4. Never add dependencies outside requirements.lock.

## Limitations
- Template codegen covers get/list/create_child shapes; other shapes need engineer authoring.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
