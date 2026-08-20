# EMI-Agent deterministic evaluation

This directory contains only synthetic development and evaluation data for the
public showcase. It contains no product, customer, laboratory, or expert-
validated measurements. No result snapshot is committed by default.

## Evidence labels

`--mode development` always emits these labels, even when an input record says
that it came from a live provider:

- `DEVELOPMENT_V1`
- `INCOMPLETE_DEVELOPMENT_ONLY`
- `NOT_EXPERT_VALIDATED`

`--mode publish` is gated and emits:

- `DEVELOPMENT_V1`
- `LIVE_SYNTHETIC_SINGLE_RUN` — applies to the 48 normal paired runs only;
- `DETERMINISTIC_REPLAY_FAULT_INJECTION` — applies to the 24 fault records;
- `NOT_EXPERT_VALIDATED`.

Fault injection is never presented as a live model run. Each fault record is a
deterministic replay sourced from its same-case, same-variant live normal run.

## Frozen data and model-input boundary

- `data/dev/cases.jsonl`: six prompt-development cases, one per EMI family.
- `data/dev/gold.jsonl`: six development answers, scoring-only.
- `data/frozen/cases.jsonl`: 24 frozen synthetic inputs, four per family.
- `data/frozen/gold.jsonl`: 24 scoring-only answers. Application runtime and
  Agent nodes must never open or receive this file.
- `data/faults/overlays.jsonl`: 12 deterministic fault definitions, balanced
  across transient error, one timeout, and process interruption.

Runtime cases contain no root-cause answer. Nevertheless, `category`, the case
ID prefix, and `required_checks` are semantically predictive in this small
synthetic set. The initial model projection must therefore exclude all of:

```text
case_id, split, category, required_checks,
observations, interventions, tool_data
```

The model may initially receive only the non-excluded case description and the
global canonical tool schemas. It does not receive per-case source IDs, exact
arguments, raw values, payloads, or `evidence_tags`. The Planner chooses the
tool, dependency, and completion condition; after that response and before plan
validation, the runtime deterministically binds `arguments` from the one
`tool_data` source for the selected tool. Each committed blind-benchmark case
contains one executable source for every canonical tool. Sources outside the
evaluator's `required_checks` carry neutral, non-gold tags: they make a generic
tool call executable but cannot satisfy the completion predicate. Baseline and
optimized runs use the same binder. Bound
arguments remain internal to the router/tool executor and the audit snapshot;
they are removed from every pre-execution model projection. Payload, source ID,
values, and tags are released to downstream Agent context only after a
successful tool call. `validate_data.py` applies the reference projection and
rejects any leakage.

Each canonical tool has exactly one explicit runtime source. `required_checks`
remains evaluator-only metadata used to validate gold and fault overlays; it is
not a hidden tool-coverage obligation for a blind Planner. Plan executability
scores selected tools, arguments, dependencies, hypothesis coverage and
completion conditions. Task completion still requires evaluator-only gold tags.

```json
{
  "source_id": "obs-1",
  "tool": "match_frequency_signature",
  "arguments": {
    "fundamental_mhz": 0.4,
    "peak_key": "obs-1",
    "tolerance_mhz": 0.02
  },
  "payload": {
    "measurement_ref": "obs-1",
    "reference_ref": "obs-2",
    "observed_frequency_mhz": 0.4,
    "fundamental_mhz": 0.4,
    "harmonic_order": 1,
    "delta_mhz": 0.0,
    "matched": true
  },
  "evidence_tags": ["switching_frequency_alignment"]
}
```

The validator requires every gold tag to have one and only one such runtime
source. The scorer then accepts the tag only from successful evidence produced
by that source's canonical tool. A model cannot satisfy evidence coverage by
printing a tag through another tool.

Canonical tools are fixed:

```text
query_measurement
match_frequency_signature
compare_intervention
inspect_coupling_path
check_measurement_consistency
```

## Canonical evaluation snapshot

The scorer consumes one JSON object per line. The benchmark adapter must export
this contract, not the internal LangGraph state shape. A minimal completed
record is shown below. Its `plan.steps[].arguments` are the audited,
deterministically bound arguments, not values independently guessed by the
model:

```json
{
  "schema_version": "2.0",
  "run_id": "opaque-run-id",
  "case_id": "EVAL-PWR-001",
  "variant": "optimized",
  "run_kind": "evaluation",
  "status": "completed",
  "plan": {
    "hypotheses": [{"hypothesis_id": "hyp-1", "text": "..."}],
    "steps": [{
      "step_id": "step-1",
      "hypothesis_id": "hyp-1",
      "tool": "match_frequency_signature",
      "arguments": {
        "fundamental_mhz": 0.4,
        "peak_key": "obs-1",
        "tolerance_mhz": 0.02
      },
      "depends_on": [],
      "completion_condition": "a tagged match result is recorded"
    }]
  },
  "evidence": [{
    "evidence_id": "stable-operation-id",
    "operation_id": "stable-operation-id",
    "step_id": "step-1",
    "tool": "match_frequency_signature",
    "status": "success",
    "phase": "initial",
    "evidence_tags": ["switching_frequency_alignment"],
    "attempt": 1
  }],
  "diagnosis": {
    "candidates": [{
      "root_cause_id": "power_input_filter_resonance",
      "rank": 1,
      "confidence": 0.82,
      "evidence_ids": ["stable-operation-id"],
      "label": "optional display label",
      "rationale": "optional final structured evidence summary"
    }],
    "claims": [{
      "claim_id": "claim-1",
      "text": "an auditable atomic conclusion",
      "evidence_ids": ["stable-operation-id"],
      "contradicting_evidence_ids": []
    }]
  },
  "review": {
    "initial_diagnosis": {"candidates": [], "claims": []},
    "issues": []
  },
  "metrics": {
    "model_calls": 4,
    "input_tokens": 1200,
    "output_tokens": 350,
    "latency_ms": 2100
  },
  "provenance": {
    "execution_mode": "live",
    "provider": "dashscope",
    "model": "qwen3.7-plus-2026-05-26",
    "config_hash": "64 hexadecimal characters",
    "prompt_hashes": {
      "planner": "64 hexadecimal characters",
      "evidence": "64 hexadecimal characters",
      "diagnosis": "64 hexadecimal characters",
      "reviewer": "64 hexadecimal characters"
    },
    "data_hash": "SHA-256 of this case's canonical JSON object"
  },
  "trajectory": []
}
```

`diagnosis.root_causes`, `cause_id`, `contradicted_by`, and mixed internal plus
adapter diagnosis shapes are rejected. Claims refer to successful evidence in
their own `evidence_ids` and `contradicting_evidence_ids`; old reverse links on
evidence are ignored. A valid contradiction takes priority over support. The
optional rationale is a final structured summary, never hidden chain-of-thought.

Evidence, claim, candidate, reviewer issue, trajectory event, and run IDs are
checked for uniqueness and all references are validated. `operation_id` is the
evidence identity; when `evidence_id` is present it must be identical.

## Reviewer semantics

The scorer reconstructs the valid initial issue universe from
`review.initial_diagnosis`, initial evidence, and deterministic plan rules. The
only valid issue type and target combinations are:

| `issue_type` | `target_id` |
|---|---|
| `unsupported_claim` | initial `claim_id` |
| `contradicted_claim` | initial `claim_id` |
| `failed_step` | plan `step_id` |
| `plan_gap` | required canonical tool name |
| `invalid_step` | plan `step_id` |

A model-omitted issue remains in the denominator and cannot count as resolved.
The issue's declared `resolved` value is not trusted: resolution is recomputed
from final successful evidence and final diagnosis. A task completes only when
the plan is executable, the top candidate is grounded, all required tools and
gold tags are covered, every final claim is supported, and no valid Reviewer
issue remains unresolved.

## Deterministic fault proof

Trajectory events use the backend event envelope:

```json
{
  "event_key": "unique-event-key",
  "node": "evidence_worker",
  "detail": {"event_type": "tool_attempt"},
  "timestamp": "ISO-8601 timestamp"
}
```

Retry overlays select the first dynamic step using their canonical tool. They
do not predict `operation_id` or `step_id`. The scorer reads both from the one
matching `fault_injected` event and requires exactly two `tool_attempt` events
with the same non-empty IDs: attempt 1 is `failure`/`timeout`, attempt 2 is
`success`, and the successful evidence has that same operation ID.

Process recovery requires one `checkpoint_resume` event with:

```json
{
  "event_type": "checkpoint_resume",
  "checkpoint_resume": {
    "from": {
      "process_instance_id": "old-instance",
      "checkpoint_id": "persisted-checkpoint"
    },
    "to": {
      "process_instance_id": "new-instance",
      "resumed_from_checkpoint_id": "persisted-checkpoint"
    }
  }
}
```

The process IDs must differ. The `fault_injected.detail.checkpoint_id`,
`checkpoint_resume.from.checkpoint_id`, and
`checkpoint_resume.to.resumed_from_checkpoint_id` must match exactly. The
overlay's `after_node` must have completed successfully in the old instance
strictly before the injected interruption, and no node completed successfully
before recovery may complete again in the new instance. Self-declared
`recovered`, `triggered`, retry-count, or checkpoint booleans are ignored.
Manual/human-intervention trajectory events fail recovery.

## Metrics and fixed denominators

- executable-plan, task-completion, and Top-1 hit rates use `/24` per variant;
  plan executability measures the Agent's tool/dependency/completion choices
  plus the shared system binder's deterministic result, not a claim that the
  model independently recovered hidden source arguments;
- invalid-step micro rate is invalid or redundant steps divided by all steps;
  macro rate reports the mean of defined per-case step rates and its denominator;
- unsupported-claim rate is unsupported plus contradicted final claims divided
  by all auditable final claims;
- Reviewer resolution is deterministically resolved valid issues divided by all
  reconstructed valid issues, or `N/A` when the denominator is zero;
- fault recovery uses `/12` per variant and requires trajectory proof, task
  completion, Top-1 correctness, no manual repair, and no duplicate operation;
- model calls, input/output tokens, and latency report count, total, mean,
  median, minimum, maximum, and range. No small-sample P95 is reported.

The stable frontend-facing summary path is
`summary.variants.baseline|optimized`, with the rate and cost names above. Every
rate includes raw numerator and denominator.

## Development and publish commands

Development accepts custom inputs but can never emit live-result labels:

```powershell
python -m evaluation `
  --mode development `
  --runs path/to/development-runs.jsonl `
  --cases evaluation/data/dev/cases.jsonl `
  --gold evaluation/data/dev/gold.jsonl `
  --faults path/to/development-overlays.jsonl `
  --output-dir path/to/development-output
```

Publish mode requires the pinned committed files, exactly 48 normal live runs
and 24 deterministic replay fault runs, and passes all gates before its first
output write:

```powershell
python -m evaluation `
  --mode publish `
  --runs path/to/complete-runs.jsonl `
  --cases evaluation/data/frozen/cases.jsonl `
  --gold evaluation/data/frozen/gold.jsonl `
  --faults evaluation/data/faults/overlays.jsonl `
  --output-dir path/to/publish-output
```

All 48 normal records must use `execution_mode=live`, the same actual model and
the same four prompt hashes. Each variant has one stable `config_hash`.
Per-run `data_hash` is recomputed from that run's original case object using
UTF-8 SHA-256 over sorted-key compact canonical JSON. The 24 fault records use
`execution_mode=replay`; `provenance.source_run_id` must point to the matching
same-case, same-variant live normal record and all hashes must match its source.

The requested model is `qwen3.7-plus-2026-05-26`. An actual model of
`qwen3.7-plus` is accepted only with
`fallback_reason=snapshot_unavailable`; otherwise requested and actual model
must match and fallback reason is empty. The manifest records requested model,
actual model, fallback reason, prompt/config/data hashes, pinned official file
hashes, input hashes, and run IDs without absolute paths or secrets.

On success the command writes `manifest.json`, `per_case.csv`, `summary.json`,
and `badcases.md`. On any completeness, provenance, hash, schema, or proof
failure it writes nothing.

## Offline verification

```powershell
python -m unittest discover -s evaluation/tests -v
python -m evaluation.validate_data
```

The integration fixture uses the real committed 24/12 data shapes in memory to
exercise the complete 48-live-plus-24-replay publication contract. It does not
write or commit benchmark results.
