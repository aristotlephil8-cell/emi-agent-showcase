from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.benchmark import serialize_benchmark_snapshot
from app.contracts import DiagnosisOutput
from app.graph import (
    GraphContext,
    _repair_diagnosis_citations,
    build_optimized_graph,
    initial_state,
    route_after_review,
)

from .helpers import (
    CASE,
    OverlapProbe,
    RecordingProvider,
    plan_response,
    standard_responses,
    supported_diagnosis,
)


async def collect_graph(
    graph: Any, state: dict[str, Any] | None, context: GraphContext, thread_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    values: dict[str, Any] = {}
    custom: list[dict[str, Any]] = []
    async for mode, chunk in graph.astream(
        state,
        config={"configurable": {"thread_id": thread_id}},
        context=context,
        stream_mode=["custom", "values"],
    ):
        if mode == "values":
            values = chunk
        elif mode == "custom":
            custom.append(chunk)
    return values, custom


@pytest.mark.asyncio
async def test_compiled_astream_runs_evidence_workers_in_parallel() -> None:
    graph = build_optimized_graph(InMemorySaver())
    probe = OverlapProbe()
    provider = RecordingProvider(standard_responses())
    state = initial_state(
        run_id="parallel-run",
        thread_id="parallel-thread",
        attempt_id="attempt-1",
        case=CASE,
    )

    final_state, events = await collect_graph(
        graph,
        state,
        GraphContext(provider=provider, profile="optimized", failure_injector=probe),
        "parallel-thread",
    )

    assert probe.max_active == 2
    assert final_state["status"] == "completed"
    assert len(final_state["evidence"]) == 2
    assert events[0]["type"] == "node_started"
    assert events[-1]["type"] == "report_ready"


@pytest.mark.asyncio
async def test_reviewer_does_not_reexecute_successful_step_for_citation_issue() -> None:
    first_diagnosis = supported_diagnosis()
    first_diagnosis["claims"][0].update(
        {"evidence_ids": [], "support_status": "unsupported"}
    )
    first_diagnosis["status"] = "insufficient_evidence"
    responses = {
        "cases": {
            "test-case": {
                "planner": plan_response(),
                "diagnosis": first_diagnosis,
                "reviewer": [
                    {
                        "issues": [
                            {
                                "issue_id": "review-s2",
                                "kind": "unsupported_claim",
                                "severity": "high",
                                "step_id": "s2",
                                "claim_id": "c1",
                                "message": "citation is missing",
                                "resolved": False,
                                "required_tool": "check_measurement_consistency",
                                "required_arguments": {
                                    "measurement_key": "b",
                                    "tolerance_percent": 10,
                                },
                            }
                        ],
                        "needs_rework": True,
                    },
                ],
            }
        }
    }
    provider = RecordingProvider(responses)
    graph = build_optimized_graph(InMemorySaver())
    state = initial_state(
        run_id="rework-run",
        thread_id="rework-thread",
        attempt_id="attempt-1",
        case=CASE,
    )

    final_state, _ = await collect_graph(
        graph,
        state,
        GraphContext(
            provider=provider,
            profile="optimized",
            failure_injector=OverlapProbe(0),
        ),
        "rework-thread",
    )

    assert provider.evidence_steps.count("s1") == 1
    assert provider.evidence_steps.count("s2") == 1
    assert final_state["rework_count"] == 0
    assert final_state["review"]["cycle"] == 1
    assert final_state["status"] == "needs_human_review"
    snapshot = serialize_benchmark_snapshot(
        final_state, mode="optimized", run_kind="evaluation"
    )
    assert snapshot["review"]["initial_diagnosis"]["claims"][0]["evidence_ids"] == []
    assert snapshot["review"]["issues"][0]["issue_type"] == "unsupported_claim"
    assert snapshot["review"]["issues"][0]["target_id"] == "c1"
    assert snapshot["review"]["issues"][0]["resolved"] is False


def test_reviewer_routes_only_an_actual_failed_step() -> None:
    state = {
        "plan": {
            "steps": [
                {
                    "step_id": "s1",
                    "tool": "query_measurement",
                    "arguments": {"key": "a"},
                }
            ]
        },
        "evidence": [{"step_id": "s1", "status": "success"}],
        "review": {
            "cycle": 1,
            "needs_rework": True,
            "issues": [
                {
                    "kind": "failed_step",
                    "step_id": "s1",
                    "required_tool": "query_measurement",
                    "required_arguments": {"key": "a"},
                    "resolved": False,
                }
            ],
        },
    }
    assert route_after_review(state) == "finalize"


def test_diagnosis_repairs_only_evidence_id_explicitly_echoed_in_rationale() -> None:
    diagnosis = supported_diagnosis()
    diagnosis["root_causes"][0]["evidence_ids"] = []
    diagnosis["root_causes"][0]["rationale"] = "see evidence-1, not a guessed source"
    diagnosis["claims"][0].update(
        {"claim_id": "tool-claim", "evidence_ids": ["unknown", "evidence-1"]}
    )
    repaired = _repair_diagnosis_citations(
        DiagnosisOutput.model_validate(diagnosis),
        [
            {
                "operation_id": "evidence-1",
                "status": "success",
                "supports_claim_ids": ["tool-claim"],
            },
            {"operation_id": "evidence-2", "status": "failure"},
        ],
    )
    assert repaired.root_causes[0].evidence_ids == ["evidence-1"]
    assert repaired.claims[0].evidence_ids == ["evidence-1"]


def test_diagnosis_binds_claims_to_the_executed_tool_support_mapping() -> None:
    diagnosis = supported_diagnosis()
    diagnosis["claims"][0].update(
        {"claim_id": "tool-claim", "evidence_ids": [], "contradicted_by": []}
    )
    repaired = _repair_diagnosis_citations(
        DiagnosisOutput.model_validate(diagnosis),
        [
            {
                "operation_id": "evidence-1",
                "status": "success",
                "supports_claim_ids": ["tool-claim"],
                "contradicts_claim_ids": [],
            },
            {
                "operation_id": "evidence-2",
                "status": "success",
                "supports_claim_ids": [],
                "contradicts_claim_ids": ["tool-claim"],
            },
        ],
    )
    assert repaired.claims[0].evidence_ids == ["evidence-1"]
    assert repaired.claims[0].contradicted_by == ["evidence-2"]


@pytest.mark.asyncio
async def test_reviewer_drops_stale_unsupported_issue_after_citation_is_fixed() -> None:
    responses = standard_responses()
    responses["cases"]["test-case"]["reviewer"] = {
        "issues": [
            {
                "issue_id": "stale-c1",
                "kind": "unsupported_claim",
                "severity": "high",
                "claim_id": "c1",
                "message": "stale finding",
                "resolved": False,
            }
        ],
        "needs_rework": True,
    }
    provider = RecordingProvider(responses)
    graph = build_optimized_graph(InMemorySaver())
    state = initial_state(
        run_id="stale-review-run",
        thread_id="stale-review-thread",
        attempt_id="attempt-1",
        case=CASE,
    )

    final_state, _ = await collect_graph(
        graph,
        state,
        GraphContext(provider=provider, profile="optimized", failure_injector=OverlapProbe(0)),
        "stale-review-thread",
    )

    assert final_state["status"] == "completed"
    assert final_state["review"]["issues"] == []


def test_benchmark_field_names_are_stable() -> None:
    plan = plan_response()
    assert set(plan["steps"][0]) >= {
        "tool",
        "arguments",
        "depends_on",
        "completion_condition",
    }


def test_benchmark_snapshot_deduplicates_model_evidence_references() -> None:
    state = {
        "run_id": "deduplicate-run",
        "case": {"case_id": "test-case"},
        "status": "completed",
        "plan": {"hypotheses": [], "steps": [{"step_id": "s1"}]},
        "evidence": [
            {"evidence_id": "evidence-1", "operation_id": "evidence-1", "status": "success", "phase": "initial"}
        ],
        "diagnosis": {
            "root_causes": [{"cause_id": "shared_return_impedance", "evidence_ids": ["evidence-1", "evidence-1"]}],
            "claims": [{"claim_id": "claim-1", "text": "deduplicated", "evidence_ids": ["evidence-1", "evidence-1"], "contradicted_by": ["evidence-1", "evidence-1"]}],
        },
        "review": {},
        "trajectory": [],
        "metrics": {},
    }
    snapshot = serialize_benchmark_snapshot(state, mode="baseline", run_kind="evaluation")
    candidate = snapshot["diagnosis"]["candidates"][0]
    claim = snapshot["diagnosis"]["claims"][0]
    assert candidate["evidence_ids"] == ["evidence-1"]
    assert claim["evidence_ids"] == ["evidence-1"]
    assert claim["contradicting_evidence_ids"] == ["evidence-1"]

    state["review"] = {
        "issues": [
            {"issue_id": "issue-1", "kind": "invalid_step", "step_id": "s1", "resolved": False, "message": "first"},
            {"issue_id": "issue-2", "kind": "invalid_step", "step_id": "s1", "resolved": False, "message": "duplicate"},
            {"issue_id": "issue-3", "kind": "plan_gap", "resolved": False, "message": "unaddressable"},
            {"issue_id": "issue-4", "kind": "unsupported_claim", "claim_id": "unknown-claim", "resolved": False, "message": "not initial"},
        ]
    }
    snapshot = serialize_benchmark_snapshot(state, mode="baseline", run_kind="evaluation")
    assert [item["issue_id"] for item in snapshot["review"]["issues"]] == ["issue-1"]


@pytest.mark.asyncio
async def test_unbound_live_tool_selection_becomes_evidence_failure() -> None:
    case = deepcopy(CASE)
    case["tool_data"] = [case["tool_data"][0]]
    responses = standard_responses()
    plan = responses["cases"]["test-case"]["planner"]
    plan["steps"][1]["tool"] = "match_frequency_signature"
    plan["steps"][1]["arguments"] = {}
    provider = RecordingProvider(responses)
    graph = build_optimized_graph(InMemorySaver())
    state = initial_state(
        run_id="unbound-tool-run",
        thread_id="unbound-tool-thread",
        attempt_id="attempt-1",
        case=case,
    )

    final_state, _ = await collect_graph(
        graph,
        state,
        GraphContext(provider=provider, profile="optimized", failure_injector=OverlapProbe(0)),
        "unbound-tool-thread",
    )

    failed = next(item for item in final_state["evidence"] if item["step_id"] == "s2")
    assert failed["status"] == "failure"
    assert failed["error"] == "tool_source_unavailable"
    assert final_state["status"] == "needs_human_review"


@pytest.mark.asyncio
async def test_reviewer_recomputes_non_actionable_rework_flag() -> None:
    responses = standard_responses()
    responses["cases"]["test-case"]["reviewer"] = {
        "issues": [],
        "needs_rework": True,
    }
    provider = RecordingProvider(responses)
    graph = build_optimized_graph(InMemorySaver())
    state = initial_state(
        run_id="advisory-review-run",
        thread_id="advisory-review-thread",
        attempt_id="attempt-1",
        case=CASE,
    )

    final_state, _ = await collect_graph(
        graph,
        state,
        GraphContext(provider=provider, profile="optimized", failure_injector=OverlapProbe(0)),
        "advisory-review-thread",
    )

    assert final_state["status"] == "completed"
    assert final_state["review"]["needs_rework"] is False


@pytest.mark.asyncio
async def test_reviewer_discards_unaddressable_invalid_step_issue() -> None:
    responses = standard_responses()
    responses["cases"]["test-case"]["reviewer"] = {
        "issues": [
            {
                "issue_id": "unaddressable-invalid-step",
                "kind": "invalid_step",
                "severity": "low",
                "message": "An invalid step was found.",
                "resolved": False,
            }
        ],
        "needs_rework": True,
    }
    provider = RecordingProvider(responses)
    graph = build_optimized_graph(InMemorySaver())
    state = initial_state(
        run_id="unaddressable-review-run",
        thread_id="unaddressable-review-thread",
        attempt_id="attempt-1",
        case=CASE,
    )

    final_state, _ = await collect_graph(
        graph,
        state,
        GraphContext(provider=provider, profile="optimized", failure_injector=OverlapProbe(0)),
        "unaddressable-review-thread",
    )

    assert final_state["review"]["issues"] == []
    assert final_state["review"]["needs_rework"] is False
