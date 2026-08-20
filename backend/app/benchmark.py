from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from .contracts import ProfileName
from .data import sanitize_case_for_execution
from .graph import (
    FailureInjector,
    GraphContext,
    NoopFailureInjector,
    build_baseline_graph,
    build_optimized_graph,
    initial_state,
)
from .providers import ModelProvider
from .prompts import ROLE_PROMPTS
from .reporting import labels_for_execution
from .tools import TOOL_INPUT_MODELS


async def run_to_completion(
    case: dict[str, Any],
    mode: ProfileName,
    provider: ModelProvider,
    *,
    checkpointer: Any | None = None,
    failure_injector: FailureInjector | None = None,
    run_kind: str = "evaluation",
    overlay_id: str | None = None,
    fault_type: str | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stable async entrypoint for evaluation runners.

    Every execution still uses a compiled graph's ``astream`` method. Callers that need
    durable fault recovery can pass an ``AsyncSqliteSaver``; ordinary paired evaluation
    runs use an isolated in-memory checkpointer.
    """

    resolved_run_id = run_id or f"evaluation-{uuid.uuid4()}"
    resolved_thread_id = thread_id or resolved_run_id
    graph = (
        build_optimized_graph(checkpointer or InMemorySaver())
        if mode == "optimized"
        else build_baseline_graph()
    )
    safe_case = sanitize_case_for_execution(case)
    state = initial_state(
        run_id=resolved_run_id,
        thread_id=resolved_thread_id,
        attempt_id=f"attempt-{uuid.uuid4()}",
        case=safe_case,
        run_kind=run_kind,  # type: ignore[arg-type] - runner validates its manifest vocabulary
        overlay_id=overlay_id,
        fault_type=fault_type,
    )
    final_state: dict[str, Any] = {}
    execution_started = time.perf_counter()
    provider_name = str(getattr(provider, "provider_name", type(provider).__name__))
    execution_mode = (
        "live"
        if "dashscope" in provider_name.lower()
        else ("replay" if run_kind == "fault_injection" else "fixture")
    )
    async for value in graph.astream(
        state,
        config={"configurable": {"thread_id": resolved_thread_id}, "recursion_limit": 32},
        context=GraphContext(
            provider=provider,
            profile=mode,
            failure_injector=failure_injector or NoopFailureInjector(),
            process_instance_id=state["attempt_id"],
            overlay_id=overlay_id,
            fault_type=fault_type,
            execution_mode=execution_mode,
        ),
        stream_mode="values",
    ):
        final_state = value
    final_state.setdefault("metrics", {})["end_to_end_latency_ms"] = round(
        (time.perf_counter() - execution_started) * 1000, 3
    )
    resolved_provenance = provenance or build_provenance(
        final_state,
        mode=mode,
        run_kind=run_kind,
        provider_name=provider_name,
        data_material=case,
    )
    return serialize_benchmark_snapshot(
        final_state,
        mode=mode,
        run_kind=run_kind,
        provenance=resolved_provenance,
    )


def serialize_benchmark_snapshot(
    state: dict[str, Any],
    *,
    mode: ProfileName,
    run_kind: str,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the JSONL-ready run contract consumed by the evaluation package."""

    successful_evidence_ids = {
        item.get("evidence_id") or item.get("operation_id")
        for item in state.get("evidence", [])
        if item.get("status") == "success"
    }
    internal_diagnosis = dict(state.get("diagnosis", {}))
    diagnosis = _canonical_diagnosis(internal_diagnosis, successful_evidence_ids)
    internal_review = dict(state.get("review", {}))
    initial_internal = internal_review.get("initial_diagnosis") or internal_diagnosis
    initial_successful_evidence_ids = {
        item.get("evidence_id") or item.get("operation_id")
        for item in state.get("evidence", [])
        if item.get("status") == "success" and item.get("phase") == "initial"
    }
    initial_diagnosis = _canonical_diagnosis(
        initial_internal, initial_successful_evidence_ids
    )
    review = {
        "initial_diagnosis": initial_diagnosis,
        "issues": _canonical_review_issues(
            internal_review.get("issues", []),
            initial_claim_ids={item["claim_id"] for item in initial_diagnosis["claims"]},
            plan_step_ids={item.get("step_id") for item in state.get("plan", {}).get("steps", [])},
        ),
    }
    fault = _derive_fault_proof(state)
    resolved_provenance = provenance or build_provenance(
        state,
        mode=mode,
        run_kind=run_kind,
        provider_name="unknown",
    )
    snapshot = {
        "run_id": state.get("run_id"),
        "case_id": state.get("case", {}).get("case_id"),
        "mode": mode,
        "variant": mode,
        "run_kind": run_kind,
        "status": "failed" if state.get("status") == "failure" else state.get("status"),
        "plan": state.get("plan") or {"hypotheses": [], "steps": []},
        "evidence": state.get("evidence", []),
        "diagnosis": diagnosis,
        "review": review,
        "trajectory": state.get("trajectory", []),
        "metrics": {
            **state.get("metrics", {}),
            "latency_ms": state.get("metrics", {}).get("end_to_end_latency_ms", 0),
        },
        "fault": fault,
        "labels": labels_for_execution(resolved_provenance["execution_mode"]),
        "provenance": resolved_provenance,
    }
    return json.loads(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))


def _canonical_diagnosis(
    diagnosis: dict[str, Any], successful_evidence_ids: set[str]
) -> dict[str, Any]:
    def unique_successful(values: list[Any]) -> list[str]:
        seen: set[str] = set()
        canonical: list[str] = []
        for value in values:
            if not isinstance(value, str) or value not in successful_evidence_ids:
                continue
            if value not in seen:
                seen.add(value)
                canonical.append(value)
        return canonical

    return {
        "candidates": [
            {
                "root_cause_id": item.get("cause_id"),
                "rank": rank,
                "label": item.get("label"),
                "confidence": item.get("confidence"),
                "evidence_ids": unique_successful(item.get("evidence_ids", [])),
                "rationale": item.get("rationale"),
            }
            for rank, item in enumerate(diagnosis.get("root_causes", []), start=1)
        ],
        "claims": [
            {
                "claim_id": claim.get("claim_id"),
                "text": claim.get("text"),
                "evidence_ids": unique_successful(claim.get("evidence_ids", [])),
                "contradicting_evidence_ids": unique_successful(
                    claim.get("contradicted_by", [])
                ),
            }
            for claim in diagnosis.get("claims", [])
        ],
    }


def _issue_target_id(issue: dict[str, Any]) -> str | None:
    issue_type = issue.get("kind")
    if issue_type in {"unsupported_claim", "contradicted_claim"}:
        return issue.get("claim_id")
    if issue_type in {"failed_step", "invalid_step"}:
        return issue.get("step_id")
    if issue_type == "plan_gap":
        return issue.get("required_tool")
    return None


def _canonical_review_issues(
    issues: list[dict[str, Any]],
    *,
    initial_claim_ids: set[str | None],
    plan_step_ids: set[str | None],
) -> list[dict[str, Any]]:
    """Preserve the first actionable issue per type/target evaluator identity."""

    canonical: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for issue in issues:
        issue_type = issue.get("kind")
        target_id = _issue_target_id(issue)
        if not target_id:
            continue
        if issue_type in {"unsupported_claim", "contradicted_claim"} and target_id not in initial_claim_ids:
            continue
        if issue_type in {"failed_step", "invalid_step"} and target_id not in plan_step_ids:
            continue
        if issue_type == "plan_gap" and target_id not in TOOL_INPUT_MODELS:
            continue
        identity = (issue_type, target_id)
        if identity in seen:
            continue
        seen.add(identity)
        canonical.append(
            {
                "issue_id": issue.get("issue_id"),
                "issue_type": issue_type,
                "target_id": target_id,
                "resolved": bool(issue.get("resolved", False)),
                "description": issue.get("message"),
            }
        )
    return canonical


def build_provenance(
    state: dict[str, Any],
    *,
    mode: ProfileName,
    run_kind: str,
    provider_name: str,
    data_material: Any | None = None,
) -> dict[str, Any]:
    models = state.get("metrics", {}).get("models", [])
    model = models[0] if len(models) == 1 else ",".join(models) or "unknown"
    inference_config = state.get("metrics", {}).get("inference_config", {})
    requested_model = inference_config.get("requested_model", model)
    provider_key = provider_name.lower()
    if "dashscope" in provider_key:
        provider = "dashscope"
        execution_mode = "live"
    else:
        provider = "fixture"
        execution_mode = "replay" if run_kind == "fault_injection" else "fixture"
    config_material = {
        "benchmark_schema_version": "2.0",
        "mode": mode,
        "run_kind": run_kind,
        "model": model,
        "requested_model": requested_model,
        "inference_config": {
            key: inference_config.get(key)
            for key in (
                "temperature",
                "enable_thinking",
                "max_tokens",
                "requested_model",
            )
        },
    }
    return {
        "provider": provider,
        "execution_mode": execution_mode,
        "model": model,
        "requested_model": requested_model,
        "actual_model": model,
        "fallback_reason": (
            "snapshot_unavailable" if model != requested_model else None
        ),
        "config_hash": _sha256(config_material),
        "prompt_hashes": {
            role: _sha256(prompt) for role, prompt in sorted(ROLE_PROMPTS.items())
        },
        "data_hash": _sha256(
            state.get("case", {}) if data_material is None else data_material
        ),
    }


def _sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _derive_fault_proof(state: dict[str, Any]) -> dict[str, Any]:
    configured = dict(state.get("metrics", {}).get("fault", {}))
    trajectory = state.get("trajectory", [])
    configured["triggered"] = any(
        bool(item.get("detail", {}).get("fault_injected")) for item in trajectory
    )
    configured.setdefault("manual_intervention", False)
    process_instances = {
        item.get("detail", {}).get("process_instance_id")
        for item in trajectory
        if item.get("detail", {}).get("process_instance_id")
    }
    max_tool_attempt = max(
        (
            int(item.get("detail", {}).get("tool_attempt") or 0)
            for item in trajectory
            if item.get("detail", {}).get("event_type")
            in {"tool_attempt", "fault_injected"}
        ),
        default=0,
    )
    configured["attempt_count"] = max(
        int(configured.get("attempt_count", 1)),
        len(process_instances) or 1,
        max_tool_attempt or 1,
    )
    configured["resumed_from_checkpoint"] = any(
        item.get("detail", {}).get("event_type") == "checkpoint_resume"
        for item in trajectory
    )
    process_by_operation: dict[str, set[str]] = {}
    for item in trajectory:
        detail = item.get("detail", {})
        if detail.get("event_type") != "tool_attempt":
            continue
        operation_id = detail.get("operation_id")
        process_instance = detail.get("process_instance_id")
        if operation_id and process_instance:
            process_by_operation.setdefault(operation_id, set()).add(process_instance)
    configured["successful_nodes_reexecuted"] = sum(
        max(0, len(processes) - 1) for processes in process_by_operation.values()
    )
    return configured
