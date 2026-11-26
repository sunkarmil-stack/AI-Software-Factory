# regulatory-reporting-analysis

Draft factual variance commentary and triage data-quality breaks for regulatory returns, using only computed figures and table metadata.

## Inputs
- material variances (cell, prior, current, change, percentage) from the reporting engine
- data-quality breaks (check, table, detail, owning team)

## Outputs
- one factual draft sentence per material variance, with no business driver
- one triage item per break: likely cause, owner, next step

## Procedure
1. For each variance, state the line, direction, change and percentage, and the prior and current values, in CAD millions.
2. Leave the driver to the preparer: never write "due to", "because" or "driven by".
3. For each data-quality break, classify the cause (missing data, failed load, late load, reconciliation difference).
4. Name the owning data team from the table metadata, and say the fix lands at source as a new table version.

## Limitations
- Never states business drivers; the preparer confirms them.
- Never computes, certifies or files figures.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
