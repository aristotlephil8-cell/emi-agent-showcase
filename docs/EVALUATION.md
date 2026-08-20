# Evaluation protocol

## Evidence labels

Only a complete 48-record normal benchmark with verified live provenance may
use this boundary:

`DEVELOPMENT_V1 / LIVE_SYNTHETIC_SINGLE_RUN / NOT_EXPERT_VALIDATED`

The 24 deterministic fault-injection records replay the corresponding live
structured outputs and add `DETERMINISTIC_REPLAY_FAULT_INJECTION`; they are not
described as live model calls. Partial, fixture, or local regression reports use
`INCOMPLETE_DEVELOPMENT / FIXTURE_OR_REPLAY` instead of the live label.

The cases are public synthetic fixtures. Results are a development snapshot,
not evidence of production performance, real-device validation, statistical
significance, or expert agreement.

## Data split

- Six development fixtures are used for prompt and contract debugging.
- Twenty-four frozen evaluation fixtures cover six EMI cause families with four
  variants per family.
- Runtime-visible cases and evaluator-only gold records are stored separately.
  Agent code and tools must never load the gold file.
- Every case contains a hidden executable source for each canonical tool. Extra
  sources emit neutral, non-gold tags, so they remove source-availability bias
  without exposing per-case tool availability to the model.
- Twelve deterministic fault overlays exercise transient tool errors, one-shot
  timeouts, and process interruption/resume without adding model randomness.
  Each overlay is paired across baseline and optimized, yielding 24 fault
  records.

## Paired comparison

Each evaluation case runs once with `baseline` and once with `optimized` under
the same model snapshot, endpoint, temperature, token limits, case input, tool
data and report schema. Profiles are interleaved by case. Every failure remains
in the raw results.

The live benchmark targets `qwen3.7-plus-2026-05-26`. If that snapshot is not
available but the rolling `qwen3.7-plus` alias is, the manifest must record the
fallback. Authentication or network failure blocks live results; replay output
must not be relabeled as live.

## Metrics

- **Plan executable rate:** plans satisfying selected-tool, argument,
  dependency, hypothesis-coverage and completion-condition rules divided by
  24. Evaluator-only required checks are not a blind-plan coverage gate.
- **Invalid step rate:** invalid or redundant steps divided by all planned
  steps, plus a per-case macro average.
- **Task completion rate:** runs satisfying the frozen completion predicate
  divided by 24.
- **Top-1 cause hit rate:** structured `cause_id` matching a frozen acceptable
  cause divided by 24.
- **Unsupported conclusion rate:** unsupported or contradicted auditable claims
  divided by all auditable claims.
- **Reviewer issue resolution:** valid issues closed without a blocking
  regression divided by valid issues; zero denominator is `N/A`.
- **Recovery success:** faults that actually trigger and then complete without
  manual repair or duplicate evidence divided by 12.
- **Cost:** model calls, input/output tokens, end-to-end mean, median, minimum and
  maximum latency.

Every rate is published with its numerator and denominator. With only 24 cases,
the report does not use P95 latency or claims of stable/general improvement.

## Required artifacts

- benchmark manifest with model/config/prompt/data hashes;
- per-run JSONL trajectories without hidden reasoning;
- per-case scoring CSV;
- machine-readable summary JSON;
- Badcase report containing every failed or regressed case;
- a command that recomputes the summary from raw records.

Credentials, local absolute paths, complete provider endpoints, and model chain
of thought are never written to artifacts.
