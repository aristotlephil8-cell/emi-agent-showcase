# EMI-Agent showcase guide

EMI-Agent is a portfolio-facing verification layer for a complex-equipment EMI
decision-support project. It separates résumé-scale project outcomes from the
smaller public V1 regression bundle, so an interviewer can inspect each claim
at the right evidence level.

## A 90-second review route

1. Read the [candidate contribution](../README.md#candidate-contribution) and
   [system flow](../README.md#system-flow): planning, stateful orchestration,
   recovery, review feedback and the engineer decision boundary.
2. Inspect the [resume evaluation highlights](../README.md#resume-evaluation-highlights):
   40 public/synthetic cases, 120 repeated runs, 60 fault injections and 280
   annotated atomic claims per profile.
3. For executable public code, inspect the [architecture](ARCHITECTURE.md),
   [evaluation protocol](EVALUATION.md), [summary](../evaluation/results/summary.json)
   and [canonical trajectories](../artifacts/runs/full-frozen-blind-v3-20260820-canonical.jsonl).
4. Run `./scripts/verify.ps1`, or use the GitHub Actions workflow. It needs no
   model credential because CI exercises fixture and deterministic replay paths.

## Two evaluation boundaries

| Evidence layer | Scope | Safe statement |
| --- | --- | --- |
| Résumé project evaluation | 40 public/synthetic cases, 120 repeated runs; 60 fault injections; 280 annotated atomic claims per profile | The candidate-reported baseline/optimized results in the README |
| Public V1 regression | 24 frozen cases; 48 live normal trajectories; 24 deterministic replay fault trajectories | The committed implementation, evaluator and recovery-proof contract are reproducible |

They differ in size, inputs and review-cycle policy. The public V1 limits
targeted rework to one round; the résumé project uses at most three. Do not
merge their numerators, denominators or percentage claims.

## Inspectable implementation mechanisms

| Mechanism | Code/evidence route |
| --- | --- |
| Executable planning | Structured plan, deterministic argument binding and plan validation |
| Parallel evidence collection | Compiled LangGraph `Send` branches with reducer-based merging |
| Fault tolerance | Stable operation IDs, bounded retry and SQLite checkpoint/resume proof |
| Review feedback | Evidence gaps/conflicts return to the affected execution path only |
| Engineering guardrails | Fixture-safe CI, locked dependencies, synthetic data and human confirmation |

## Claim boundary

The public repository is not an online product demo. It contains synthetic
data only, no private-device material or credentials, and no claim of
production reliability, real-device effectiveness, expert endorsement or
automatic engineering decisions.
