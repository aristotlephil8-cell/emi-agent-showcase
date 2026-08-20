# EMI-Agent architecture

EMI-Agent is a decision-support demo for evidence-grounded electromagnetic
interference diagnosis. It ranks candidate causes and proposes verification
actions; an engineer remains responsible for the final decision.

![EMI-Agent public V1 architecture](assets/emi-agent-architecture.svg)

The diagram is derived from `backend/app/graph.py` (`route_parallel_evidence`,
`route_after_review` and `build_optimized_graph`) and `backend/app/runtime.py`
(`astream`, SQLite checkpoint setup and SSE envelopes). It depicts public V1's
at-most-one targeted rework policy, not the broader résumé project protocol.

## Runtime invariants

- The API invokes the compiled LangGraph with `astream`; there is no manual
  orchestration fallback.
- Nodes return partial state updates. They do not mutate shared state in place.
- Parallel evidence branches merge through reducers and stable evidence IDs.
- Checkpoint state contains JSON-compatible values only. Providers, credentials,
  stream writers and fault injectors stay in runtime context.
- An unresolved critical review issue produces `needs_human_review`, never a
  false successful diagnosis.
- Tools are read-only and allowlisted. The project does not execute generated
  code, browse private systems, or modify equipment.

## Comparison profiles

| Capability | Baseline | Optimized |
| --- | --- | --- |
| Planning | One structured pass | Validation feedback and one correction |
| Evidence | Serial execution | Parallel `Send` branches |
| Retry/recovery | Disabled | Bounded retry and SQLite checkpoint |
| Duplicate suppression | None | Stable operation/evidence IDs |
| Independent review | Disabled | One targeted rework round |

Both profiles use the same cases, model snapshot, tool registry, output schemas,
deterministic reporter and evaluator. Extra calls, tokens and latency are
reported as costs rather than hidden.

## Deliberate boundaries

This development version excludes RAG, authentication, multi-user deployment,
vector databases, arbitrary code execution, cancellation, and SSE history
replay. Those capabilities are not required to demonstrate the four Agent
mechanisms and would weaken same-day reproducibility.
