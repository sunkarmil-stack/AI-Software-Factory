# rag-evaluation

Evaluate retrieval and answers separately: passage recall, answer correctness and citation support.

## Inputs
- benchmark set
- pinned release

## Outputs
- per-category scores

## Procedure
1. Measure recall@k against labelled relevant passages.
2. Score answer correctness independently of fluency.
3. Check each citation supports the claim it is attached to.
4. Include adversarial and cross-tenant cases.

## Limitations
- Scores from small benchmarks have wide variance; report sample sizes.

Requested capabilities are requests only; the runtime identity and policy decide what is granted.
