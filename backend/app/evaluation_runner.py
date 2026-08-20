from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .benchmark import (
    build_provenance,
    run_to_completion,
    serialize_benchmark_snapshot,
)
from .config import Settings
from .contracts import ProfileName, ProviderResponse, ToolName
from .data import load_json_object, sanitize_case_for_execution
from .graph import (
    FailureInjector,
    GraphContext,
    InjectedRunCrash,
    NoopFailureInjector,
    build_optimized_graph,
    initial_state,
)
from .providers import DashScopeProvider, FixtureReplayProvider, ModelProvider
from .tools import TOOL_REGISTRY, ToolExecutionError


ProviderFactory = Callable[[], ModelProvider]
FaultType = Literal["transient_tool_error", "timeout_once", "process_interrupt"]


def append_jsonl_fsync(path: Path, record: dict[str, Any]) -> None:
    """Append one complete record and force it to stable storage."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class StructuredRecordingProvider:
    """Record schema-constrained provider outputs only; never requests, secrets, or endpoints."""

    def __init__(
        self,
        delegate: ModelProvider,
        output_path: Path,
        *,
        variant: ProfileName,
        source_run_id: str,
    ) -> None:
        self._delegate = delegate
        self._output_path = output_path
        self._lock = asyncio.Lock()
        self.provider_name = str(
            getattr(delegate, "provider_name", type(delegate).__name__)
        )
        self._variant = variant
        self._source_run_id = source_run_id
        self._call_sequence = 0

    @property
    def total_provider_attempts(self) -> int | None:
        value = getattr(self._delegate, "total_provider_attempts", None)
        return int(value) if isinstance(value, int) else None

    async def complete(
        self,
        *,
        role: str,
        case_id: str,
        input_data: dict[str, Any],
        schema: dict[str, Any],
    ) -> ProviderResponse:
        response = await self._delegate.complete(
            role=role,
            case_id=case_id,
            input_data=input_data,
            schema=schema,
        )
        async with self._lock:
            self._call_sequence += 1
            record = {
                "recording_id": str(uuid.uuid4()),
                "source_run_id": self._source_run_id,
                "call_sequence": self._call_sequence,
                "case_id": case_id,
                "role": role,
                "variant": self._variant,
                "data": _replace_evidence_ids_with_placeholders(
                    response.data, input_data
                ),
                "usage": response.usage.model_dump(mode="json"),
                "model": response.model,
                "inference_config": response.inference_config,
                "content_policy": "STRUCTURED_OUTPUT_ONLY_NO_COT",
            }
            append_jsonl_fsync(self._output_path, record)
        return response


def _replace_evidence_ids_with_placeholders(value: Any, input_data: dict[str, Any]) -> Any:
    placeholder_by_id = {
        str(item["operation_id"]): f"{{{{evidence:{item['step_id']}}}}}"
        for item in input_data.get("evidence", [])
        if isinstance(item, dict) and item.get("operation_id") and item.get("step_id")
    }

    def replace(item: Any) -> Any:
        if isinstance(item, str):
            return placeholder_by_id.get(item, item)
        if isinstance(item, list):
            return [replace(child) for child in item]
        if isinstance(item, dict):
            return {key: replace(child) for key, child in item.items()}
        return item

    return replace(value)


class UsageTrackingProvider:
    """Count attempted calls and known usage even when a run terminates early."""

    def __init__(self, delegate: ModelProvider) -> None:
        self._delegate = delegate
        self.provider_name = str(
            getattr(delegate, "provider_name", type(delegate).__name__)
        )
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.models: set[str] = set()
        self.inference_config: dict[str, Any] = {}

    async def complete(
        self,
        *,
        role: str,
        case_id: str,
        input_data: dict[str, Any],
        schema: dict[str, Any],
    ) -> ProviderResponse:
        before_attempts = getattr(self._delegate, "total_provider_attempts", None)
        try:
            response = await self._delegate.complete(
                role=role,
                case_id=case_id,
                input_data=input_data,
                schema=schema,
            )
        finally:
            after_attempts = getattr(self._delegate, "total_provider_attempts", None)
            if isinstance(before_attempts, int) and isinstance(after_attempts, int):
                self.model_calls += max(1, after_attempts - before_attempts)
            else:
                self.model_calls += 1
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        self.models.add(response.model)
        self.inference_config = response.inference_config
        return response


async def run_one(
    case: dict[str, Any],
    variant: ProfileName,
    provider: ModelProvider,
    *,
    run_kind: str = "evaluation",
    overlay_id: str | None = None,
    fault_type: str | None = None,
    failure_injector: FailureInjector | None = None,
    run_id: str | None = None,
    source_run_id: str | None = None,
) -> dict[str, Any]:
    resolved_run_id = run_id or f"evaluation-{uuid.uuid4()}"
    started = time.perf_counter()
    tracked_provider = (
        provider if isinstance(provider, UsageTrackingProvider) else UsageTrackingProvider(provider)
    )
    try:
        snapshot = await run_to_completion(
            case,
            variant,
            tracked_provider,
            run_kind=run_kind,
            overlay_id=overlay_id,
            fault_type=fault_type,
            failure_injector=failure_injector,
            run_id=resolved_run_id,
        )
    except Exception as exc:
        snapshot = _build_failure_snapshot(
            case,
            variant,
            tracked_provider,
            run_id=resolved_run_id,
            run_kind=run_kind,
            overlay_id=overlay_id,
            fault_type=fault_type,
            error_type=type(exc).__name__,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            failure_injector=failure_injector,
            source_run_id=source_run_id,
        )
    if source_run_id:
        snapshot["provenance"]["source_run_id"] = source_run_id
    return snapshot


def _build_failure_snapshot(
    case: dict[str, Any],
    variant: ProfileName,
    provider: ModelProvider,
    *,
    run_id: str,
    run_kind: str,
    overlay_id: str | None,
    fault_type: str | None,
    error_type: str,
    elapsed_ms: float,
    failure_injector: FailureInjector | None,
    source_run_id: str | None,
) -> dict[str, Any]:
    attempt_id = f"attempt-{uuid.uuid4()}"
    state = initial_state(
        run_id=run_id,
        thread_id=run_id,
        attempt_id=attempt_id,
        case=sanitize_case_for_execution(case),
        run_kind=run_kind,  # type: ignore[arg-type]
        overlay_id=overlay_id,
        fault_type=fault_type,
    )
    detail: dict[str, Any] = {
        "event_type": "run_failed",
        "node": "runner",
        "outcome": "failure",
        "error_type": error_type,
        "overlay_id": overlay_id,
        "fault_type": fault_type,
        "tool": None,
        "step_id": None,
        "operation_id": None,
        "tool_attempt": None,
        "process_instance_id": attempt_id,
        "checkpoint_id": None,
        "fault_injected": False,
    }
    if isinstance(failure_injector, OverlayFailureInjector) and failure_injector.triggered:
        detail.update(failure_injector.proof_detail(process_instance_id=attempt_id))
        detail["event_type"] = "fault_injected"
        detail["outcome"] = error_type
        detail["fault_injected"] = True
    state["trajectory"] = [
        {
            "event_key": "99:runner-failure",
            "node": str(detail.get("node") or "runner"),
            "detail": detail,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    ]
    state["status"] = "failure"
    state["metrics"]["end_to_end_latency_ms"] = round(elapsed_ms, 3)
    if isinstance(provider, UsageTrackingProvider):
        state["metrics"].update(
            {
                "model_calls": provider.model_calls,
                "input_tokens": provider.input_tokens,
                "output_tokens": provider.output_tokens,
                "models": sorted(provider.models),
                "inference_config": provider.inference_config,
            }
        )
    provenance = build_provenance(
        state,
        mode=variant,
        run_kind=run_kind,
        provider_name=str(getattr(provider, "provider_name", type(provider).__name__)),
        data_material=case,
    )
    if source_run_id:
        provenance["source_run_id"] = source_run_id
    return serialize_benchmark_snapshot(
        state,
        mode=variant,
        run_kind=run_kind,
        provenance=provenance,
    )


async def run_interleaved(
    cases: Sequence[dict[str, Any]],
    provider_factory: ProviderFactory,
    output_path: Path,
    *,
    variants: Sequence[ProfileName] = ("baseline", "optimized"),
    recording_path: Path | None = None,
    rerun_failures: bool = False,
) -> list[dict[str, Any]]:
    """Run case-major, variant-interleaved evaluation and durably append every outcome."""

    completed = _completed_keys(output_path, rerun_failures=rerun_failures)
    emitted: list[dict[str, Any]] = []
    for case_index, case in enumerate(cases):
        ordered_variants = tuple(variants)
        if case_index % 2:
            ordered_variants = tuple(reversed(ordered_variants))
        for variant in ordered_variants:
            key = (str(case.get("case_id")), variant, "evaluation", None)
            if key in completed:
                continue
            run_id = f"evaluation-{uuid.uuid4()}"
            provider: ModelProvider = provider_factory()
            if recording_path is not None:
                provider = StructuredRecordingProvider(
                    provider,
                    recording_path,
                    variant=variant,
                    source_run_id=run_id,
                )
            snapshot = await run_one(case, variant, provider, run_id=run_id)
            append_jsonl_fsync(output_path, snapshot)
            emitted.append(snapshot)
            completed.add(key)
    return emitted


@dataclass(frozen=True)
class FaultScenario:
    case_id: str
    overlay_id: str
    fault_type: FaultType
    tool: ToolName | None = None
    selector: Literal["first_matching_tool"] | None = None
    target_node: str = "evidence_worker"
    after_node: str | None = None


class OverlayFailureInjector:
    """Inject exactly once at the first runtime operation matching a canonical tool."""

    def __init__(self, scenario: FaultScenario) -> None:
        self.scenario = scenario
        self.triggered = False
        self.matched_context: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    async def before_tool(
        self,
        *,
        tool: str,
        step_id: str,
        operation_id: str,
        attempt: int,
    ) -> None:
        async with self._lock:
            if (
                self.scenario.fault_type == "process_interrupt"
                or self.triggered
                or tool != self.scenario.tool
            ):
                return
            self.triggered = True
            self.matched_context = {
                "tool": tool,
                "step_id": step_id,
                "operation_id": operation_id,
                "tool_attempt": attempt,
            }
        if self.scenario.fault_type == "transient_tool_error":
            raise ToolExecutionError("injected_transient_tool_error", transient=True)
        if self.scenario.fault_type == "timeout_once":
            raise asyncio.TimeoutError
        raise AssertionError("unreachable fault type")

    async def before_node(self, *, node: str) -> None:
        if self.scenario.fault_type != "process_interrupt":
            return
        async with self._lock:
            if self.triggered or node != self.scenario.target_node:
                return
            self.triggered = True
            self.matched_context = {"node": node}
        raise InjectedRunCrash("injected_process_interrupt")

    def proof_detail(
        self,
        *,
        process_instance_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        context = self.matched_context or {}
        return {
            "overlay_id": self.scenario.overlay_id,
            "fault_type": self.scenario.fault_type,
            "tool": context.get("tool", self.scenario.tool),
            "step_id": context.get("step_id"),
            "operation_id": context.get("operation_id"),
            "tool_attempt": context.get("tool_attempt", 1),
            "process_instance_id": process_instance_id,
            "checkpoint_id": checkpoint_id,
            "node": context.get("node", self.scenario.target_node),
        }


async def run_fault_replays(
    cases: Sequence[dict[str, Any]],
    scenarios: Sequence[FaultScenario],
    recordings_path: Path,
    source_runs_path: Path,
    output_path: Path,
    *,
    variants: Sequence[ProfileName] = ("baseline", "optimized"),
    checkpoint_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Run deterministic structured-output replays for each overlay and both variants."""

    case_by_id = {str(case["case_id"]): case for case in cases}
    source_runs = _source_run_index(source_runs_path)
    completed = _completed_keys(output_path, rerun_failures=False)
    emitted: list[dict[str, Any]] = []
    resolved_checkpoint_dir = checkpoint_dir or Settings().checkpoint_db.parent / "faults"
    for scenario_index, scenario in enumerate(scenarios):
        case = case_by_id[scenario.case_id]
        ordered_variants = tuple(variants)
        if scenario_index % 2:
            ordered_variants = tuple(reversed(ordered_variants))
        for variant in ordered_variants:
            key = (scenario.case_id, variant, "fault_injection", scenario.overlay_id)
            if key in completed:
                continue
            source_run = source_runs[(scenario.case_id, variant)]
            source_run_id = str(source_run["run_id"])
            responses = load_recorded_responses(
                recordings_path,
                variant=variant,
                source_run_id=source_run_id,
            )
            provider_factory = lambda value=responses: FixtureReplayProvider(value)
            run_id = f"fault-{scenario.overlay_id}-{variant}-{uuid.uuid4()}"
            if scenario.fault_type == "process_interrupt" and variant == "optimized":
                snapshot = await _run_process_interrupt_replay(
                    case,
                    variant,
                    scenario,
                    provider_factory,
                    run_id=run_id,
                    source_run=source_run,
                    checkpoint_dir=resolved_checkpoint_dir,
                )
            else:
                snapshot = await run_one(
                    case,
                    variant,
                    provider_factory(),
                    run_kind="fault_injection",
                    overlay_id=scenario.overlay_id,
                    fault_type=scenario.fault_type,
                    failure_injector=OverlayFailureInjector(scenario),
                    run_id=run_id,
                    source_run_id=source_run_id,
                )
                _apply_replay_provenance(snapshot, source_run)
            append_jsonl_fsync(output_path, snapshot)
            emitted.append(snapshot)
            completed.add(key)
    return emitted


async def _run_process_interrupt_replay(
    case: dict[str, Any],
    variant: ProfileName,
    scenario: FaultScenario,
    provider_factory: ProviderFactory,
    *,
    run_id: str,
    source_run: dict[str, Any],
    checkpoint_dir: Path,
) -> dict[str, Any]:
    if variant != "optimized":
        raise ValueError("process checkpoint resume is available only for optimized mode")
    source_run_id = str(source_run["run_id"])
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    db_path = checkpoint_dir / f"{run_id}.sqlite"
    thread_id = run_id
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 32}
    first_process = f"process-1-{uuid.uuid4()}"
    second_process = f"process-2-{uuid.uuid4()}"
    state = initial_state(
        run_id=run_id,
        thread_id=thread_id,
        attempt_id=first_process,
        case=sanitize_case_for_execution(case),
        run_kind="fault_injection",
        overlay_id=scenario.overlay_id,
        fault_type=scenario.fault_type,
    )
    injector = OverlayFailureInjector(scenario)
    execution_started = time.perf_counter()
    checkpoint_id: str | None = None

    first_connection = await aiosqlite.connect(db_path)
    first_saver = AsyncSqliteSaver(first_connection, serde=_strict_serde())
    await first_saver.setup()
    first_graph = build_optimized_graph(first_saver)
    try:
        async for _ in first_graph.astream(
            state,
            config=config,
            context=GraphContext(
                provider=provider_factory(),
                profile="optimized",
                failure_injector=injector,
                process_instance_id=first_process,
                overlay_id=scenario.overlay_id,
                fault_type=scenario.fault_type,
                execution_mode="replay",
            ),
            stream_mode="values",
        ):
            pass
    except InjectedRunCrash:
        interrupted = await first_graph.aget_state(config)
        checkpoint_id = interrupted.config.get("configurable", {}).get("checkpoint_id")
    finally:
        await first_connection.close()
    if not injector.triggered or not checkpoint_id:
        failed = _build_failure_snapshot(
            case,
            variant,
            provider_factory(),
            run_id=run_id,
            run_kind="fault_injection",
            overlay_id=scenario.overlay_id,
            fault_type=scenario.fault_type,
            error_type="checkpoint_interrupt_not_observed",
            elapsed_ms=(time.perf_counter() - execution_started) * 1000,
            failure_injector=injector,
            source_run_id=source_run_id,
        )
        _apply_replay_provenance(failed, source_run)
        return failed

    second_connection = await aiosqlite.connect(db_path)
    second_saver = AsyncSqliteSaver(second_connection, serde=_strict_serde())
    await second_saver.setup()
    second_graph = build_optimized_graph(second_saver)
    final_state: dict[str, Any] = {}
    second_provider = provider_factory()
    try:
        async for value in second_graph.astream(
            None,
            config=config,
            context=GraphContext(
                provider=second_provider,
                profile="optimized",
                failure_injector=NoopFailureInjector(),
                process_instance_id=second_process,
                checkpoint_id=checkpoint_id,
                overlay_id=scenario.overlay_id,
                fault_type=scenario.fault_type,
                execution_mode="replay",
            ),
            stream_mode="values",
        ):
            final_state = value
    finally:
        await second_connection.close()

    crash_detail = {
        "event_type": "fault_injected",
        "fault_injected": True,
        "outcome": "process_interrupted",
        "node": scenario.target_node,
        **injector.proof_detail(
            process_instance_id=first_process, checkpoint_id=checkpoint_id
        ),
    }
    final_state.setdefault("trajectory", []).append(
        {
            "event_key": (
                f"28:fault:{scenario.overlay_id}"
                if scenario.target_node == "diagnosis_agent"
                else f"38:fault:{scenario.overlay_id}"
            ),
            "node": scenario.target_node,
            "detail": crash_detail,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    final_state["trajectory"] = sorted(
        final_state["trajectory"], key=lambda item: str(item.get("event_key", ""))
    )
    final_state.setdefault("metrics", {})["end_to_end_latency_ms"] = round(
        (time.perf_counter() - execution_started) * 1000, 3
    )
    snapshot = serialize_benchmark_snapshot(
        final_state,
        mode=variant,
        run_kind="fault_injection",
    )
    _apply_replay_provenance(snapshot, source_run)
    return snapshot


def _strict_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=None,
    )


def _source_run_index(path: Path) -> dict[tuple[str, ProfileName], dict[str, Any]]:
    indexed: dict[tuple[str, ProfileName], dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            record = json.loads(raw_line)
            if record.get("run_kind") != "evaluation" or record.get("status") == "failure":
                continue
            variant = str(record.get("variant") or record.get("mode"))
            if variant not in {"baseline", "optimized"}:
                continue
            indexed[(str(record["case_id"]), variant)] = record
    return indexed


def _apply_replay_provenance(
    snapshot: dict[str, Any], source_run: dict[str, Any]
) -> None:
    source = dict(source_run.get("provenance", {}))
    snapshot["provenance"] = {
        **source,
        "execution_mode": "replay",
        "source_run_id": str(source_run["run_id"]),
    }


def _completed_keys(
    output_path: Path, *, rerun_failures: bool
) -> set[tuple[str, str, str, str | None]]:
    if not output_path.is_file():
        return set()
    completed: set[tuple[str, str, str, str | None]] = set()
    with output_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            record = json.loads(raw_line)
            if rerun_failures and record.get("status") == "failure":
                continue
            completed.add(
                (
                    str(record.get("case_id")),
                    str(record.get("variant") or record.get("mode")),
                    str(record.get("run_kind", "evaluation")),
                    record.get("overlay_id") or record.get("fault", {}).get("overlay_id"),
                )
            )
    return completed


def load_cases(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            if raw_line.strip():
                value = json.loads(raw_line)
                if not isinstance(value, dict):
                    raise ValueError("each case line must be a JSON object")
                cases.append(value)
                if limit is not None and len(cases) >= limit:
                    break
    return cases


def load_recorded_responses(
    path: Path, *, variant: ProfileName, source_run_id: str
) -> dict[str, Any]:
    """Convert structured live recordings into FixtureReplayProvider input."""

    grouped: dict[str, dict[str, list[tuple[int, dict[str, Any]]]]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            if not raw_line.strip():
                continue
            record = json.loads(raw_line)
            if (
                record.get("variant") != variant
                or record.get("source_run_id") != source_run_id
            ):
                continue
            if record.get("role") == "evidence":
                continue
            case_roles = grouped.setdefault(str(record["case_id"]), {})
            case_roles.setdefault(str(record["role"]), []).append(
                (int(record["call_sequence"]), record["data"])
            )
    if not grouped:
        raise ValueError(f"recording_source_run_not_found:{source_run_id}:{variant}")
    normalized: dict[str, dict[str, Any]] = {}
    for case_id, roles in grouped.items():
        normalized_roles: dict[str, Any] = {}
        for role, values in roles.items():
            ordered = [item[1] for item in sorted(values, key=lambda item: item[0])]
            normalized_roles[role] = ordered[0] if len(ordered) == 1 else ordered
        normalized[case_id] = normalized_roles
    return {"fixture_kind": "RECORDED_STRUCTURED_OUTPUT_REPLAY", "cases": normalized}


def load_fault_scenarios(path: Path, limit: int | None = None) -> list[FaultScenario]:
    raw_text = path.read_text(encoding="utf-8")
    stripped = raw_text.strip()
    if not stripped:
        raise ValueError("fault overlay file is empty")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        raw_items = parsed
    elif isinstance(parsed, dict):
        raw_items = parsed.get("overlays", [parsed])
    else:
        raw_items = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
    if not isinstance(raw_items, list):
        raise ValueError("fault overlays must be a list")
    scenarios: list[FaultScenario] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("fault overlay must be an object")
        target = raw.get("target", {})
        if not isinstance(target, dict):
            raise ValueError("fault target must be an object")
        selector = target.get("selector")
        tool = target.get("tool")
        fault_type = raw.get("fault_type")
        if fault_type not in {
            "transient_tool_error",
            "timeout_once",
            "process_interrupt",
        }:
            raise ValueError(f"unknown fault type: {fault_type}")
        target_node = str(target.get("node") or "")
        after_node = target.get("after_node")
        if fault_type == "process_interrupt":
            if target_node not in {"diagnosis_agent", "reviewer_agent"}:
                raise ValueError("unsupported process interrupt target node")
            if not isinstance(after_node, str) or not after_node:
                raise ValueError("process interrupt requires after_node")
            tool = None
            selector = None
        else:
            if target_node != "evidence_worker":
                raise ValueError("fault target node must be evidence_worker")
            if selector != "first_matching_tool":
                raise ValueError("fault selector must be first_matching_tool")
            if tool not in TOOL_REGISTRY:
                raise ValueError(f"unknown fault tool: {tool}")
        scenarios.append(
            FaultScenario(
                case_id=str(raw["case_id"]),
                overlay_id=str(raw["overlay_id"]),
                fault_type=fault_type,
                tool=tool,
                selector=selector,
                target_node=target_node,
                after_node=after_node,
            )
        )
        if limit is not None and len(scenarios) >= limit:
            break
    overlay_ids = [item.overlay_id for item in scenarios]
    if len(overlay_ids) != len(set(overlay_ids)):
        raise ValueError("duplicate overlay_id")
    return scenarios


def _provider_factory(settings: Settings, provider_name: str) -> ProviderFactory:
    if provider_name == "fixture":
        responses = load_json_object(settings.fixture_file)
        return lambda: FixtureReplayProvider(responses)
    if not settings.dashscope_api_key:
        raise RuntimeError("dashscope_credentials_missing")
    return lambda: DashScopeProvider(
        api_key=settings.dashscope_api_key or "",
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_model,
        timeout_seconds=settings.request_timeout_seconds,
        max_tokens=settings.model_max_tokens,
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run paired EMI-Agent evaluation trajectories")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recordings", type=Path)
    parser.add_argument("--faults", type=Path)
    parser.add_argument("--recordings-replay", type=Path)
    parser.add_argument("--source-runs", type=Path)
    parser.add_argument("--checkpoint-dir", type=Path)
    parser.add_argument("--provider", choices=("fixture", "dashscope"), default="fixture")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--rerun-failures", action="store_true")
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    settings = Settings()
    cases = load_cases(arguments.cases, arguments.limit)
    if arguments.faults:
        if not arguments.recordings_replay or not arguments.source_runs:
            parser.error("--faults requires --recordings-replay and --source-runs")
        scenarios = load_fault_scenarios(arguments.faults, arguments.limit)
        asyncio.run(
            run_fault_replays(
                cases,
                scenarios,
                arguments.recordings_replay,
                arguments.source_runs,
                arguments.output,
                checkpoint_dir=arguments.checkpoint_dir,
            )
        )
    else:
        asyncio.run(
            run_interleaved(
                cases,
                _provider_factory(settings, arguments.provider),
                arguments.output,
                recording_path=arguments.recordings,
                rerun_failures=arguments.rerun_failures,
            )
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
