# EMI-Agent showcase guide

This page is a fast, evidence-first route through the public project. It is a
development showcase for synthetic EMI diagnosis, not a claim of laboratory,
customer, production or expert-validated performance.

## A 90-second review route

1. Start with the two UI captures in the [README](../README.md#demo). They show
   the same public surface as the local fixture demo: run trajectory, evidence
   and counter-evidence, candidate causes, Reviewer feedback and the required
   engineer decision.
2. Read the [architecture](ARCHITECTURE.md) comparison table. It states exactly
   what differs between `baseline` and `optimized`; both profiles retain the
   same model snapshot, cases, tool registry, schemas and reporter.
3. Inspect the [evaluation protocol](EVALUATION.md) and the
   [summary](../evaluation/results/summary.json). Every outcome is published as
   a numerator and denominator, alongside model-call and latency costs.
4. If a result matters, trace it back to the [canonical JSONL
   records](../artifacts/runs/full-frozen-blind-v3-20260820-canonical.jsonl) and
   [Badcase report](../evaluation/results/badcases.md), then rerun the local
   checks from the README.

## Implemented engineering mechanisms

| Mechanism | Observable implementation evidence |
| --- | --- |
| Executable planning | Structured plan, deterministic argument binding and plan validation before tool execution |
| Parallel evidence collection | Compiled LangGraph `Send` branches with reducer-based stable evidence merging |
| Fault tolerance | Stable operation IDs, bounded retry and SQLite checkpoint/resume proof |
| Evidence-grounded diagnosis | Candidate causes and atomic claims reference successful evidence and counter-evidence |
| Targeted quality control | Reviewer identifies the failed claim or step and permits at most one directed rework |
| Honest comparison | Paired baseline/optimized runs report effects together with calls, tokens and latency |

## Published snapshot: how to read it

The committed snapshot has 48 credentialed live normal trajectories and 24
deterministic replay fault trajectories. It is labeled
`DEVELOPMENT_V1 / LIVE_SYNTHETIC_SINGLE_RUN /
DETERMINISTIC_REPLAY_FAULT_INJECTION / NOT_EXPERT_VALIDATED`.

The optimized profile improved executable plans (`18/24` to `22/24`), completed
tasks (`13/24` to `18/24`), unsupported or contradicted claims (`1/56` to
`0/56`) and replayed fault recovery (`0/12` to `6/12`). Its model calls and
mean end-to-end latency also increased (`139` to `172`; `35,382 ms` to
`41,733 ms`). Top-1 cause hits were `23/24` versus `21/24`; the project does
not hide that regression or claim a globally better model.

## Claim boundary

This repository supports these limited statements:

- an independently implemented, runnable Agent workflow exists locally;
- its public evaluation is synthetic, frozen and recomputable from committed
  trajectories;
- the listed metrics are one development snapshot with explicit denominators;
- final engineering decisions stay with a human reviewer.

It does **not** support claims of statistical significance, production
reliability, real-device effectiveness, expert endorsement or universal
performance improvement. Historic résumé-scale case/run counts are intentionally
not used as repository results unless they are regenerated under this protocol.
