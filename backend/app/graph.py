from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send
from pydantic import ValidationError

from .contracts import (
    DiagnosisOutput,
    EvidenceRecord,
    PlanOutput,
    PlanStep,
    ProfileName,
    ReviewIssue,
    ReviewOutput,
    ROOT_CAUSE_TAXONOMY,
    ToolInvocation,
)
from .data import project_case_for_provider
from .providers import ModelProvider
from .reporting import build_report
from .tools import (
    TOOL_INPUT_MODELS,
    ToolExecutionError,
    execute_tool,
    validate_tool_arguments,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def merge_records(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {item["operation_id"]: item for item in left}
    for item in right:
        current = merged.get(item["operation_id"])
        if current is None or item.get("attempt", 0) >= current.get("attempt", 0):
            merged[item["operation_id"]] = item
    return sorted(merged.values(), key=lambda item: item["operation_id"])


def merge_trajectory(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged = {item["event_key"]: item for item in left}
    merged.update({item["event_key"]: item for item in right})
    return sorted(merged.values(), key=lambda item: item["event_key"])


def merge_metrics(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    integer_additive = {
        "model_calls",
        "input_tokens",
        "output_tokens",
        "tool_calls",
        "tool_retries",
    }
    for key, value in right.items():
        if key in integer_additive:
            merged[key] = int(merged.get(key, 0)) + int(value)
        elif key == "node_time_ms":
            merged[key] = round(float(merged.get(key, 0)) + float(value), 3)
        elif key == "models":
            merged[key] = sorted(set(merged.get(key, [])) | set(value))
        else:
            merged[key] = value
    return merged


class GraphState(TypedDict, total=False):
    run_id: str
    thread_id: str
    attempt_id: str
    case: dict[str, Any]
    hypotheses: list[dict[str, Any]]
    plan: dict[str, Any]
    evidence: Annotated[list[dict[str, Any]], merge_records]
    diagnosis: dict[str, Any]
    review: dict[str, Any]
    rework_count: int
    status: str
    trajectory: Annotated[list[dict[str, Any]], merge_trajectory]
    metrics: Annotated[dict[str, Any], merge_metrics]


class EvidenceTask(TypedDict):
    run_id: str
    thread_id: str
    attempt_id: str
    case: dict[str, Any]
    evidence: list[dict[str, Any]]
    active_step: dict[str, Any]
    is_rework: bool
    review_cycle: int


class FailureInjector(Protocol):
    async def before_tool(
        self,
        *,
        tool: str,
        step_id: str,
        operation_id: str,
        attempt: int,
    ) -> None: ...


class NoopFailureInjector:
    async def before_tool(
        self,
        *,
        tool: str,
        step_id: str,
        operation_id: str,
        attempt: int,
    ) -> None:
        del tool, step_id, operation_id, attempt


class InjectedRunCrash(RuntimeError):
    pass


@dataclass(frozen=True)
class GraphContext:
    provider: ModelProvider
    profile: ProfileName
    failure_injector: FailureInjector
    evidence_delay_seconds: float = 0.0
    process_instance_id: str | None = None
    checkpoint_id: str | None = None
    overlay_id: str | None = None
    fault_type: str | None = None
    execution_mode: Literal["live", "fixture", "replay"] = "fixture"


PROOF_NODE_NAMES = {
    "planner": "planner_agent",
    "diagnosis": "diagnosis_agent",
    "reviewer": "reviewer_agent",
    "finalize": "deterministic_reporter",
}


def _proof_node(node: str) -> str:
    return PROOF_NODE_NAMES.get(node, node)


async def _before_node(runtime: Runtime[GraphContext], node: str) -> None:
    hook = getattr(runtime.context.failure_injector, "before_node", None)
    if hook is not None:
        await hook(node=_proof_node(node))


def _emit(event_type: str, node: str, payload: dict[str, Any] | None = None) -> None:
    writer = get_stream_writer()
    writer({"type": event_type, "node": node, "payload": payload or {}})


def _trajectory(event_key: str, node: str, detail: dict[str, Any]) -> dict[str, Any]:
    proof_node = _proof_node(node)
    normalized_detail = dict(detail)
    if "node" in normalized_detail:
        normalized_detail["node"] = proof_node
    return {
        "event_key": event_key,
        "node": proof_node,
        "detail": normalized_detail,
        "timestamp": utc_now(),
    }


def _checkpoint_resume_trajectory(
    state: GraphState,
    runtime: Runtime[GraphContext],
    *,
    node: str,
    event_key: str,
) -> list[dict[str, Any]]:
    process_instance = runtime.context.process_instance_id or state["attempt_id"]
    already_recorded = any(
        item.get("detail", {}).get("event_type") == "checkpoint_resume"
        for item in state.get("trajectory", [])
    )
    if (
        state["attempt_id"] == process_instance
        or already_recorded
        or not runtime.context.checkpoint_id
    ):
        return []
    proof_node = _proof_node(node)
    checkpoint_id = runtime.context.checkpoint_id
    return [
        _trajectory(
            event_key,
            node,
            {
                "event_type": "checkpoint_resume",
                "overlay_id": runtime.context.overlay_id,
                "fault_type": runtime.context.fault_type,
                "tool": None,
                "step_id": None,
                "operation_id": None,
                "fault_injected": False,
                "tool_attempt": 0,
                "outcome": "resumed",
                "process_instance_id": process_instance,
                "checkpoint_id": checkpoint_id,
                "node": proof_node,
                "checkpoint_resume": {
                    "from": {
                        "process_instance_id": state["attempt_id"],
                        "checkpoint_id": checkpoint_id,
                    },
                    "to": {
                        "process_instance_id": process_instance,
                        "resumed_from_checkpoint_id": checkpoint_id,
                    },
                },
            },
        )
    ]


def _provider_metrics(response: Any, elapsed_ms: float) -> dict[str, Any]:
    return {
        "model_calls": int(response.inference_config.get("provider_attempts", 1)),
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "node_time_ms": round(elapsed_ms, 3),
        "models": [response.model],
        "inference_config": response.inference_config,
    }


def validate_plan(plan: PlanOutput) -> list[str]:
    errors: list[str] = []
    step_index = {step.step_id: index for index, step in enumerate(plan.steps)}
    covered: set[str] = set()
    for step in plan.steps:
        covered.add(step.hypothesis_id)
        try:
            validate_tool_arguments(step.tool, step.arguments)
        except ValidationError:
            errors.append(f"{step.step_id}:invalid_tool_arguments")
        for dependency in step.depends_on:
            if step_index[dependency] >= step_index[step.step_id]:
                errors.append(f"{step.step_id}:dependency_not_prior:{dependency}")
    for hypothesis in plan.hypotheses:
        if hypothesis.hypothesis_id not in covered:
            errors.append(f"{hypothesis.hypothesis_id}:not_covered")
    return sorted(set(errors))


def _resolved_tool_arguments(
    case: dict[str, Any], tool: str, proposed: dict[str, Any]
) -> dict[str, Any]:
    candidates = [
        item
        for item in case.get("tool_data", [])
        if isinstance(item, dict)
        and item.get("tool") == tool
        and isinstance(item.get("arguments"), dict)
    ]
    if len(candidates) == 1:
        return json.loads(json.dumps(candidates[0]["arguments"], ensure_ascii=False))
    del proposed
    return {}


def _bind_plan_arguments(plan: PlanOutput, case: dict[str, Any]) -> PlanOutput:
    return plan.model_copy(
        update={
            "steps": [
                step.model_copy(
                    update={
                        "arguments": _resolved_tool_arguments(
                            case, step.tool, step.arguments
                        )
                    }
                )
                for step in plan.steps
            ]
        }
    )


async def planner_node(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    await _before_node(runtime, "planner")
    started = time.perf_counter()
    _emit("node_started", "planner", {"profile": runtime.context.profile})
    input_data: dict[str, Any] = {
        "case": project_case_for_provider(state["case"]),
        "allowed_tools": [
            {
                "name": tool,
                "arguments_schema": TOOL_INPUT_MODELS[tool].model_json_schema(),
            }
            for tool in sorted(TOOL_INPUT_MODELS)
        ],
    }
    metrics: dict[str, Any] = {}
    corrected = False
    validation_errors: list[str]
    try:
        response = await runtime.context.provider.complete(
            role="planner",
            case_id=state["case"]["case_id"],
            input_data=input_data,
            schema=PlanOutput.model_json_schema(),
        )
        metrics = merge_metrics(
            metrics, _provider_metrics(response, (time.perf_counter() - started) * 1000)
        )
        plan = _bind_plan_arguments(PlanOutput.model_validate(response.data), state["case"])
        validation_errors = validate_plan(plan)
    except ValidationError as exc:
        if runtime.context.profile == "baseline":
            raise
        validation_errors = [f"schema:{item['type']}" for item in exc.errors()]
        plan = None

    if validation_errors and runtime.context.profile == "optimized":
        corrected = True
        correction_started = time.perf_counter()
        correction_input = {**input_data, "validation_errors": validation_errors}
        response = await runtime.context.provider.complete(
            role="planner",
            case_id=state["case"]["case_id"],
            input_data=correction_input,
            schema=PlanOutput.model_json_schema(),
        )
        metrics = merge_metrics(
            metrics,
            _provider_metrics(response, (time.perf_counter() - correction_started) * 1000),
        )
        plan = _bind_plan_arguments(PlanOutput.model_validate(response.data), state["case"])
        validation_errors = validate_plan(plan)
    if plan is None:  # defensive; optimized correction either set it or raised above
        raise RuntimeError("planner_output_unavailable")

    plan_data = plan.model_dump(mode="json")
    plan_data.update(
        {
            "validation_errors": validation_errors,
            "executable": not validation_errors,
            "corrected": corrected,
        }
    )
    _emit(
        "node_completed",
        "planner",
        {
            "step_count": len(plan.steps),
            "executable": not validation_errors,
            "plan": plan_data,
            "hypotheses": [item.model_dump(mode="json") for item in plan.hypotheses],
        },
    )
    return {
        "hypotheses": [item.model_dump(mode="json") for item in plan.hypotheses],
        "plan": plan_data,
        "status": "executing",
        "trajectory": [
            _trajectory(
                "10:planner",
                "planner",
                {
                    "event_type": "node_completed",
                    "step_count": len(plan.steps),
                    "validation_errors": validation_errors,
                    "node": "planner",
                    "outcome": "success",
                    "process_instance_id": runtime.context.process_instance_id
                    or state["attempt_id"],
                },
            )
        ],
        "metrics": metrics,
    }


def route_parallel_evidence(state: GraphState) -> list[Send]:
    return [
        Send(
            "evidence_worker",
            {
                "run_id": state["run_id"],
                "thread_id": state["thread_id"],
                "attempt_id": state["attempt_id"],
                "case": state["case"],
                "evidence": state.get("evidence", []),
                "active_step": step,
                "is_rework": False,
                "review_cycle": 0,
            },
        )
        for step in state["plan"]["steps"]
    ]


def _operation_id(thread_id: str, step_id: str) -> str:
    return hashlib.sha256(f"{thread_id}:{step_id}".encode("utf-8")).hexdigest()[:20]


def _known_evidence_ids(records: list[dict[str, Any]]) -> set[str]:
    return {
        str(item["operation_id"])
        for item in records
        if item.get("status") == "success" and item.get("operation_id")
    }


def _repair_diagnosis_citations(
    diagnosis: DiagnosisOutput, records: list[dict[str, Any]]
) -> DiagnosisOutput:
    """Keep only successful citations and bind IDs declared by the executed tool."""

    known_ids = _known_evidence_ids(records)
    supports_by_claim: dict[str, list[str]] = {}
    contradictions_by_claim: dict[str, list[str]] = {}
    for record in records:
        operation_id = str(record.get("operation_id", ""))
        if record.get("status") != "success" or operation_id not in known_ids:
            continue
        for claim_id in record.get("supports_claim_ids", []):
            supports_by_claim.setdefault(str(claim_id), []).append(operation_id)
        for claim_id in record.get("contradicts_claim_ids", []):
            contradictions_by_claim.setdefault(str(claim_id), []).append(operation_id)

    def known(values: list[str]) -> list[str]:
        return sorted({value for value in values if value in known_ids})

    repaired_causes = []
    for cause in diagnosis.root_causes:
        rationale_ids = [
            evidence_id for evidence_id in known_ids if evidence_id in cause.rationale
        ]
        repaired_causes.append(
            cause.model_copy(update={"evidence_ids": known(cause.evidence_ids + rationale_ids)})
        )
    repaired_claims = [
        claim.model_copy(
            update={
                "evidence_ids": known(
                    claim.evidence_ids + supports_by_claim.get(claim.claim_id, [])
                ),
                "contradicted_by": known(
                    claim.contradicted_by
                    + contradictions_by_claim.get(claim.claim_id, [])
                ),
            }
        )
        for claim in diagnosis.claims
    ]
    return diagnosis.model_copy(
        update={"root_causes": repaired_causes, "claims": repaired_claims}
    )


async def execute_evidence_task(
    task: EvidenceTask, runtime: Runtime[GraphContext]
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    step = PlanStep.model_validate(task["active_step"])
    operation_id = _operation_id(task["thread_id"], step.step_id)
    existing = next(
        (item for item in task.get("evidence", []) if item["operation_id"] == operation_id),
        None,
    )
    if existing and existing.get("status") == "success" and not task["is_rework"]:
        _emit("evidence_deduplicated", "evidence_worker", {"step_id": step.step_id})
        return existing, {"deduplicated": True}, []

    _emit(
        "node_started",
        "evidence_worker",
        {"step_id": step.step_id, "is_rework": task["is_rework"]},
    )
    started_at = utc_now()
    started = time.perf_counter()
    provider_response = await runtime.context.provider.complete(
        role="evidence",
        case_id=task["case"]["case_id"],
        input_data={
            "case": project_case_for_provider(task["case"]),
            "step": {
                **step.model_dump(mode="json"),
                "arguments": {},
            },
        },
        schema=ToolInvocation.model_json_schema(),
    )
    invocation = ToolInvocation.model_validate(provider_response.data)
    invocation = invocation.model_copy(
        update={
            "arguments": _resolved_tool_arguments(
                task["case"], invocation.tool, invocation.arguments
            )
        }
    )
    if runtime.context.evidence_delay_seconds:
        await asyncio.sleep(runtime.context.evidence_delay_seconds)

    attempt = int(existing.get("attempt", 0) if existing else 0) + 1
    max_tool_attempts = 2 if runtime.context.profile == "optimized" else 1
    tool_retries = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    proof_events: list[dict[str, Any]] = []
    process_instance = runtime.context.process_instance_id or task["attempt_id"]
    for tool_attempt in range(1, max_tool_attempts + 1):
        proof_detail = {
            "event_type": "tool_attempt",
            "overlay_id": runtime.context.overlay_id,
            "fault_type": runtime.context.fault_type,
            "tool": invocation.tool,
            "step_id": step.step_id,
            "operation_id": operation_id,
            "fault_injected": False,
            "tool_attempt": tool_attempt,
            "outcome": "started",
            "process_instance_id": process_instance,
            "checkpoint_id": runtime.context.checkpoint_id,
            "node": "evidence_worker",
        }
        _emit("tool_attempt", "evidence_worker", proof_detail)
        try:
            if not invocation.arguments:
                raise ToolExecutionError("tool_source_unavailable")
            validate_tool_arguments(invocation.tool, invocation.arguments)
            await runtime.context.failure_injector.before_tool(
                tool=invocation.tool,
                step_id=step.step_id,
                operation_id=operation_id,
                attempt=tool_attempt,
            )
            result = await execute_tool(invocation.tool, task["case"], invocation.arguments)
            proof_events.append(
                _trajectory(
                    f"21:attempt:{task['review_cycle']:02}:{step.step_id}:{tool_attempt}",
                    "evidence_worker",
                    {**proof_detail, "outcome": "success"},
                )
            )
            break
        except (ToolExecutionError, ValidationError, asyncio.TimeoutError) as exc:
            error = exc.code if isinstance(exc, ToolExecutionError) else type(exc).__name__
            attempt_outcome = (
                "timeout" if isinstance(exc, asyncio.TimeoutError) else "failure"
            )
            proof_events.append(
                _trajectory(
                    f"21:attempt:{task['review_cycle']:02}:{step.step_id}:{tool_attempt}",
                    "evidence_worker",
                    {**proof_detail, "outcome": attempt_outcome},
                )
            )
            fault_detail = {
                **proof_detail,
                "event_type": "fault_injected",
                "fault_injected": True,
                "outcome": attempt_outcome,
                "error_code": error,
            }
            _emit("fault_injected", "evidence_worker", fault_detail)
            proof_events.append(
                _trajectory(
                    f"22:fault:{task['review_cycle']:02}:{step.step_id}:{tool_attempt}",
                    "evidence_worker",
                    fault_detail,
                )
            )
            retryable = isinstance(exc, asyncio.TimeoutError) or (
                isinstance(exc, ToolExecutionError) and exc.transient
            )
            if tool_attempt >= max_tool_attempts or not retryable:
                break
            tool_retries += 1

    finished_at = utc_now()
    if result is None:
        record = EvidenceRecord(
            operation_id=operation_id,
            evidence_id=operation_id,
            step_id=step.step_id,
            hypothesis_id=step.hypothesis_id,
            tool=invocation.tool,
            status="failure",
            phase="rework" if task["is_rework"] else "initial",
            observations=[],
            supports_claim_ids=[],
            contradicts_claim_ids=[],
            evidence_tags=[],
            started_at=started_at,
            finished_at=finished_at,
            attempt=attempt,
            error=error or "tool_failed",
        )
    else:
        record = EvidenceRecord(
            operation_id=operation_id,
            evidence_id=operation_id,
            step_id=step.step_id,
            hypothesis_id=step.hypothesis_id,
            tool=invocation.tool,
            status="success",
            phase="rework" if task["is_rework"] else "initial",
            observations=result["observations"],
            supports_claim_ids=result["supports"],
            contradicts_claim_ids=result["contradicts"],
            evidence_tags=sorted(set(result.get("evidence_tags", []))),
            started_at=started_at,
            finished_at=finished_at,
            attempt=attempt,
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics = merge_metrics(
        _provider_metrics(provider_response, elapsed_ms),
        {"tool_calls": 1 + tool_retries, "tool_retries": tool_retries},
    )
    _emit(
        "node_completed",
        "evidence_worker",
        {
            "step_id": step.step_id,
            "status": record.status,
            "attempt": attempt,
            "record": record.model_dump(mode="json"),
        },
    )
    return record.model_dump(mode="json"), metrics, proof_events


async def evidence_worker_node(
    task: EvidenceTask, runtime: Runtime[GraphContext]
) -> dict[str, Any]:
    await _before_node(runtime, "evidence_worker")
    record, metrics, proof_events = await execute_evidence_task(task, runtime)
    step_id = task["active_step"]["step_id"]
    cycle = task["review_cycle"]
    process_instance = runtime.context.process_instance_id or task["attempt_id"]
    if task["attempt_id"] != process_instance:
        proof_events.append(
            _trajectory(
                f"05:checkpoint-resume:{process_instance}:{step_id}",
                "evidence_worker",
                {
                    "event_type": "checkpoint_resume",
                    "overlay_id": runtime.context.overlay_id,
                    "fault_type": runtime.context.fault_type,
                    "tool": record["tool"],
                    "step_id": step_id,
                    "operation_id": record["operation_id"],
                    "fault_injected": True,
                    "tool_attempt": 0,
                    "outcome": "resumed",
                    "process_instance_id": process_instance,
                    "checkpoint_id": runtime.context.checkpoint_id,
                    "node": "evidence_worker",
                    "checkpoint_resume": {
                        "from": {
                            "process_instance_id": task["attempt_id"],
                            "checkpoint_id": runtime.context.checkpoint_id,
                        },
                        "to": {
                            "process_instance_id": process_instance,
                            "resumed_from_checkpoint_id": runtime.context.checkpoint_id,
                        },
                    },
                },
            )
        )
    return {
        "evidence": [record],
        "trajectory": proof_events + [
            _trajectory(
                f"20:evidence:{cycle:02}:{step_id}",
                "evidence_worker",
                {
                    "event_type": "node_completed",
                    "step_id": step_id,
                    "status": record["status"],
                    "is_rework": task["is_rework"],
                    "operation_id": record["operation_id"],
                    "process_instance_id": process_instance,
                    "checkpoint_id": runtime.context.checkpoint_id,
                    "tool_attempt": record["attempt"],
                    "fault_injected": False,
                    "overlay_id": runtime.context.overlay_id,
                    "fault_type": runtime.context.fault_type,
                    "tool": record["tool"],
                    "outcome": record["status"],
                    "node": "evidence_worker",
                },
            )
        ],
        "metrics": metrics,
    }


async def serial_evidence_node(
    state: GraphState, runtime: Runtime[GraphContext]
) -> dict[str, Any]:
    await _before_node(runtime, "evidence_serial")
    records: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    proof_events: list[dict[str, Any]] = []
    for step in state["plan"]["steps"]:
        record, task_metrics, task_proof = await execute_evidence_task(
            {
                "run_id": state["run_id"],
                "thread_id": state["thread_id"],
                "attempt_id": state["attempt_id"],
                "case": state["case"],
                "evidence": records,
                "active_step": step,
                "is_rework": False,
                "review_cycle": 0,
            },
            runtime,
        )
        records = merge_records(records, [record])
        metrics = merge_metrics(metrics, task_metrics)
        proof_events.extend(task_proof)
    return {
        "evidence": records,
        "trajectory": proof_events + [
            _trajectory(
                "20:evidence:serial",
                "evidence_serial",
                {
                    "event_type": "node_completed",
                    "step_count": len(records),
                    "node": "evidence_serial",
                    "outcome": "success",
                    "process_instance_id": runtime.context.process_instance_id
                    or state["attempt_id"],
                },
            )
        ],
        "metrics": metrics,
    }


async def diagnosis_node(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    await _before_node(runtime, "diagnosis")
    cycle = int(state.get("review", {}).get("cycle", 0))
    started = time.perf_counter()
    _emit("node_started", "diagnosis", {"cycle": cycle})
    citation_ledger = [
        {
            "evidence_id": item["operation_id"],
            "status": item["status"],
            "tool": item["tool"],
            "observations": item.get("observations", []),
            "evidence_tags": item.get("evidence_tags", []),
            "supports_claim_ids": item.get("supports_claim_ids", []),
            "contradicts_claim_ids": item.get("contradicts_claim_ids", []),
        }
        for item in state.get("evidence", [])
        if item.get("status") == "success"
    ]
    response = await runtime.context.provider.complete(
        role="diagnosis",
        case_id=state["case"]["case_id"],
        input_data={
            "case": project_case_for_provider(state["case"]),
            "hypotheses": state.get("hypotheses", []),
            "evidence": state.get("evidence", []),
            "citation_ledger": citation_ledger,
            "previous_review": state.get("review", {}),
            "allowed_root_causes": ROOT_CAUSE_TAXONOMY,
        },
        schema=DiagnosisOutput.model_json_schema(),
    )
    diagnosis = _repair_diagnosis_citations(
        DiagnosisOutput.model_validate(response.data), state.get("evidence", [])
    )
    _emit(
        "node_completed",
        "diagnosis",
        {
            "top_cause_id": diagnosis.root_causes[0].cause_id,
            "cycle": cycle,
            "diagnosis": diagnosis.model_dump(mode="json"),
        },
    )
    return {
        "diagnosis": diagnosis.model_dump(mode="json"),
        "trajectory": _checkpoint_resume_trajectory(
            state, runtime, node="diagnosis", event_key="29:checkpoint-resume:diagnosis"
        ) + [
            _trajectory(
                f"30:diagnosis:{cycle:02}",
                "diagnosis",
                {
                    "event_type": "node_completed",
                    "top_cause_id": diagnosis.root_causes[0].cause_id,
                    "node": "diagnosis",
                    "outcome": "success",
                    "process_instance_id": runtime.context.process_instance_id
                    or state["attempt_id"],
                },
            )
        ],
        "metrics": _provider_metrics(response, (time.perf_counter() - started) * 1000),
    }


def _deterministic_review_issues(
    state: GraphState, covered_claim_ids: set[str]
) -> list[ReviewIssue]:
    known_evidence = {
        item["operation_id"]: item for item in state.get("evidence", [])
    }
    issues: list[ReviewIssue] = []
    step_by_id = {
        step["step_id"]: PlanStep.model_validate(step) for step in state["plan"]["steps"]
    }
    for record in state.get("evidence", []):
        if record.get("status") == "success":
            continue
        step = step_by_id[record["step_id"]]
        issues.append(
            ReviewIssue(
                issue_id=f"auto-failed-{step.step_id}",
                kind="failed_step",
                severity="high",
                step_id=step.step_id,
                message="Evidence tool step failed and requires targeted retry.",
                required_tool=step.tool,
                required_arguments=step.arguments,
            )
        )
    for claim in state.get("diagnosis", {}).get("claims", []):
        if claim.get("claim_id") in covered_claim_ids:
            continue
        evidence_ids = claim.get("evidence_ids", [])
        supported = bool(evidence_ids) and all(
            evidence_id in known_evidence
            and known_evidence[evidence_id].get("status") == "success"
            for evidence_id in evidence_ids
        )
        if claim.get("support_status") != "supported" or not supported:
            issues.append(
                ReviewIssue(
                    issue_id=f"auto-{claim['claim_id']}",
                    kind=(
                        "contradicted_claim"
                        if claim.get("support_status") == "contradicted"
                        else "unsupported_claim"
                    ),
                    severity="high",
                    claim_id=claim["claim_id"],
                    message="Claim lacks a successful referenced evidence record.",
                )
            )
    return issues


async def reviewer_node(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    await _before_node(runtime, "reviewer")
    cycle = int(state.get("review", {}).get("cycle", 0)) + 1
    started = time.perf_counter()
    _emit("node_started", "reviewer", {"cycle": cycle})
    response = await runtime.context.provider.complete(
        role="reviewer",
        case_id=state["case"]["case_id"],
        input_data={
            "case": project_case_for_provider(state["case"]),
            "plan": state["plan"],
            "evidence": state.get("evidence", []),
            "diagnosis": state["diagnosis"],
            "review_cycle": cycle,
        },
        schema=ReviewOutput.model_json_schema(),
    )
    review = ReviewOutput.model_validate(response.data)
    issue_by_id = {issue.issue_id: issue for issue in review.issues}
    for issue in _deterministic_review_issues(state, set()):
        issue_by_id.setdefault(issue.issue_id, issue)

    known_steps = {step["step_id"]: step for step in state["plan"]["steps"]}
    successful_evidence_ids = _known_evidence_ids(state.get("evidence", []))
    claim_statuses = {
        claim["claim_id"]: (
            "contradicted"
            if set(claim.get("contradicted_by", [])) & successful_evidence_ids
            else "supported"
            if set(claim.get("evidence_ids", [])) & successful_evidence_ids
            else "unsupported"
        )
        for claim in state.get("diagnosis", {}).get("claims", [])
    }
    normalized: list[ReviewIssue] = []
    for issue in issue_by_id.values():
        # An ``invalid_step`` finding is only auditable when it identifies the
        # rejected plan step.  A model-only advisory without that identifier
        # cannot be routed, scored, or acted on, so it must not enter state.
        if issue.kind == "invalid_step" and not issue.step_id:
            continue
        if issue.kind in {"unsupported_claim", "contradicted_claim"}:
            status = claim_statuses.get(issue.claim_id or "")
            if status is None or status == "supported":
                continue
        if issue.step_id and issue.step_id not in known_steps:
            normalized.append(
                issue.model_copy(
                    update={
                        "kind": "invalid_step",
                        "required_tool": None,
                        "required_arguments": {},
                    }
                )
            )
            continue
        if issue.required_tool:
            try:
                validate_tool_arguments(issue.required_tool, issue.required_arguments)
            except ValidationError:
                issue = issue.model_copy(
                    update={"required_tool": None, "required_arguments": {}}
                )
        normalized.append(issue)

    failed_step_ids = {
        item["step_id"]
        for item in state.get("evidence", [])
        if item.get("status") != "success"
    }
    for index, issue in enumerate(normalized):
        if issue.kind != "failed_step" or issue.step_id not in failed_step_ids:
            normalized[index] = issue.model_copy(
                update={"required_tool": None, "required_arguments": {}}
            )

    if cycle > 1:
        current_ids = {issue.issue_id for issue in normalized}
        for previous in state.get("review", {}).get("issues", []):
            if previous.get("issue_id") in current_ids:
                continue
            normalized.append(
                ReviewIssue.model_validate(previous).model_copy(update={"resolved": True})
            )

    actionable = any(
        not issue.resolved
        and issue.kind == "failed_step"
        and issue.step_id in failed_step_ids
        and issue.required_tool
        for issue in normalized
    )
    review_data = {
        "issues": [item.model_dump(mode="json") for item in sorted(normalized, key=lambda x: x.issue_id)],
        "needs_rework": actionable,
        "cycle": cycle,
        "initial_diagnosis": state.get("review", {}).get("initial_diagnosis")
        or state["diagnosis"],
    }
    rework_count = int(state.get("rework_count", 0))
    if cycle == 1 and actionable:
        rework_count += 1
    _emit(
        "node_completed",
        "reviewer",
        {
            "cycle": cycle,
            "issue_count": len(normalized),
            "needs_rework": actionable,
            "review": review_data,
        },
    )
    return {
        "review": review_data,
        "rework_count": rework_count,
        "trajectory": _checkpoint_resume_trajectory(
            state, runtime, node="reviewer", event_key="39:checkpoint-resume:reviewer"
        ) + [
            _trajectory(
                f"40:reviewer:{cycle:02}",
                "reviewer",
                {
                    "event_type": "node_completed",
                    "issue_count": len(normalized),
                    "needs_rework": actionable,
                    "node": "reviewer",
                    "outcome": "success",
                    "process_instance_id": runtime.context.process_instance_id
                    or state["attempt_id"],
                },
            )
        ],
        "metrics": _provider_metrics(response, (time.perf_counter() - started) * 1000),
    }


def route_after_review(state: GraphState) -> str | list[Send]:
    review = state["review"]
    if review.get("cycle") != 1 or not review.get("needs_rework"):
        return "finalize"
    step_by_id = {step["step_id"]: step for step in state["plan"]["steps"]}
    failed_step_ids = {
        item["step_id"]
        for item in state.get("evidence", [])
        if item.get("status") != "success"
    }
    sends: list[Send] = []
    seen_steps: set[str] = set()
    for issue in review.get("issues", []):
        step_id = issue.get("step_id")
        if (
            not step_id
            or step_id in seen_steps
            or issue.get("resolved")
            or issue.get("kind") != "failed_step"
            or step_id not in failed_step_ids
            or not issue.get("required_tool")
        ):
            continue
        original = dict(step_by_id[step_id])
        original["tool"] = issue["required_tool"]
        original["arguments"] = issue.get("required_arguments", {})
        sends.append(
            Send(
                "evidence_worker",
                {
                    "run_id": state["run_id"],
                    "thread_id": state["thread_id"],
                    "attempt_id": state["attempt_id"],
                    "case": state["case"],
                    "evidence": state.get("evidence", []),
                    "active_step": original,
                    "is_rework": True,
                    "review_cycle": 1,
                },
            )
        )
        seen_steps.add(step_id)
    return sends or "finalize"


async def finalize_node(state: GraphState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    await _before_node(runtime, "finalize")
    _emit("node_started", "finalize", {})
    report, status = build_report(
        dict(state), execution_mode=runtime.context.execution_mode
    )
    diagnosis = dict(state.get("diagnosis", {}))
    diagnosis["report"] = report
    _emit("report_ready", "finalize", {"status": status})
    return {
        "diagnosis": diagnosis,
        "status": status,
        "trajectory": [
            _trajectory(
                "50:finalize",
                "finalize",
                {
                    "event_type": "node_completed",
                    "status": status,
                    "node": "finalize",
                    "outcome": "success",
                    "process_instance_id": runtime.context.process_instance_id
                    or state["attempt_id"],
                },
            )
        ],
    }


def build_optimized_graph(checkpointer: Any) -> Any:
    builder = StateGraph(GraphState, context_schema=GraphContext)
    builder.add_node("planner", planner_node)
    builder.add_node("evidence_worker", evidence_worker_node)
    builder.add_node("diagnosis", diagnosis_node)
    builder.add_node("reviewer", reviewer_node)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "planner")
    builder.add_conditional_edges("planner", route_parallel_evidence, ["evidence_worker"])
    builder.add_edge("evidence_worker", "diagnosis")
    builder.add_edge("diagnosis", "reviewer")
    builder.add_conditional_edges(
        "reviewer", route_after_review, ["evidence_worker", "finalize"]
    )
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)


def build_baseline_graph() -> Any:
    builder = StateGraph(GraphState, context_schema=GraphContext)
    builder.add_node("planner", planner_node)
    builder.add_node("evidence_serial", serial_evidence_node)
    builder.add_node("diagnosis", diagnosis_node)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "planner")
    builder.add_edge("planner", "evidence_serial")
    builder.add_edge("evidence_serial", "diagnosis")
    builder.add_edge("diagnosis", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def initial_state(
    *,
    run_id: str,
    thread_id: str,
    attempt_id: str,
    case: dict[str, Any],
    run_kind: Literal["demo", "evaluation", "fault_injection"] = "demo",
    overlay_id: str | None = None,
    fault_type: str | None = None,
) -> GraphState:
    # Deliberately plain JSON-compatible values only.
    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "attempt_id": attempt_id,
        "case": json.loads(json.dumps(case, ensure_ascii=False)),
        "hypotheses": [],
        "plan": {},
        "evidence": [],
        "diagnosis": {},
        "review": {},
        "rework_count": 0,
        "status": "planning",
        "trajectory": [],
        "metrics": {
            "run_kind": run_kind,
            "fault": {
                "overlay_id": overlay_id,
                "fault_type": fault_type,
                "triggered": False,
                "manual_intervention": False,
                "attempt_count": 1,
                "resumed_from_checkpoint": False,
                "successful_nodes_reexecuted": 0,
            },
        },
    }
