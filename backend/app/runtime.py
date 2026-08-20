from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .config import Settings
from .benchmark import build_provenance, serialize_benchmark_snapshot
from .contracts import DecisionRequest, ProviderName, RunRequest
from .data import CaseCatalog, load_json_object
from .graph import (
    GraphContext,
    NoopFailureInjector,
    build_baseline_graph,
    build_optimized_graph,
    initial_state,
)
from .providers import DashScopeProvider, FixtureReplayProvider, ModelProvider, ProviderError
from .reporting import EVIDENCE_LABELS


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RunNotFoundError(KeyError):
    pass


class InvalidRunStateError(RuntimeError):
    pass


class RunRegistry:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS emi_runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL UNIQUE,
                attempt_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                profile TEXT NOT NULL,
                provider TEXT NOT NULL,
                run_kind TEXT NOT NULL,
                overlay_id TEXT,
                fault_type TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT,
                decision_json TEXT,
                state_json TEXT
            );
            CREATE TABLE IF NOT EXISTS emi_run_attempts (
                attempt_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT,
                FOREIGN KEY(run_id) REFERENCES emi_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_emi_attempts_run ON emi_run_attempts(run_id, started_at);
            """
        )
        await self._conn.commit()

    async def create(self, session: "RunSession") -> None:
        now = utc_now()
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO emi_runs (
                    run_id, thread_id, attempt_id, case_id, profile, provider,
                    run_kind, overlay_id, fault_type, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.run_id,
                    session.thread_id,
                    session.attempt_id,
                    session.case_id,
                    session.profile,
                    session.provider_name,
                    session.run_kind,
                    session.overlay_id,
                    session.fault_type,
                    "created",
                    now,
                    now,
                ),
            )
            await self._insert_attempt(session.run_id, session.attempt_id, "created", now)
            await self._conn.commit()

    async def start_resume(self, run_id: str, attempt_id: str) -> None:
        now = utc_now()
        async with self._lock:
            await self._conn.execute(
                "UPDATE emi_runs SET attempt_id=?, status=?, updated_at=?, error=NULL WHERE run_id=?",
                (attempt_id, "resuming", now, run_id),
            )
            await self._insert_attempt(run_id, attempt_id, "created", now)
            await self._conn.commit()

    async def _insert_attempt(
        self, run_id: str, attempt_id: str, status: str, started_at: str
    ) -> None:
        await self._conn.execute(
            "INSERT INTO emi_run_attempts (attempt_id, run_id, status, started_at) VALUES (?, ?, ?, ?)",
            (attempt_id, run_id, status, started_at),
        )

    async def mark(
        self,
        run_id: str,
        attempt_id: str,
        status: str,
        *,
        error: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        state_json = (
            json.dumps(state, ensure_ascii=False, sort_keys=True) if state is not None else None
        )
        async with self._lock:
            await self._conn.execute(
                """
                UPDATE emi_runs
                SET status=?, updated_at=?, error=?, state_json=COALESCE(?, state_json)
                WHERE run_id=? AND attempt_id=?
                """,
                (status, now, error, state_json, run_id, attempt_id),
            )
            await self._conn.execute(
                """
                UPDATE emi_run_attempts
                SET status=?, finished_at=CASE WHEN ? IN ('completed','failed','interrupted') THEN ? ELSE finished_at END,
                    error=?
                WHERE attempt_id=?
                """,
                (status, status, now, error, attempt_id),
            )
            await self._conn.commit()

    async def get(self, run_id: str) -> dict[str, Any]:
        cursor = await self._conn.execute("SELECT * FROM emi_runs WHERE run_id=?", (run_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise RunNotFoundError(run_id)
        record = dict(row)
        decision_json = record.pop("decision_json")
        state_json = record.pop("state_json")
        record["decision"] = json.loads(decision_json) if decision_json else None
        record["state"] = json.loads(state_json) if state_json else None
        attempts_cursor = await self._conn.execute(
            "SELECT attempt_id, status, started_at, finished_at, error FROM emi_run_attempts "
            "WHERE run_id=? ORDER BY started_at, attempt_id",
            (run_id,),
        )
        record["attempts"] = [dict(item) for item in await attempts_cursor.fetchall()]
        await attempts_cursor.close()
        return record

    async def decide(self, run_id: str, decision: DecisionRequest) -> None:
        now = utc_now()
        payload = json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        async with self._lock:
            cursor = await self._conn.execute(
                "UPDATE emi_runs SET decision_json=?, updated_at=? WHERE run_id=?",
                (payload, now, run_id),
            )
            await self._conn.commit()
            if cursor.rowcount == 0:
                raise RunNotFoundError(run_id)


@dataclass(frozen=True)
class RunSession:
    run_id: str
    thread_id: str
    attempt_id: str
    case_id: str
    profile: str
    provider_name: str
    run_kind: str
    overlay_id: str | None
    fault_type: str | None
    case: dict[str, Any]
    resume: bool = False
    checkpoint_id: str | None = None


class EventEnvelopeFactory:
    def __init__(self, session: RunSession):
        self._session = session
        self._sequence = 0

    def create(self, event_type: str, node: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._sequence += 1
        return {
            "event_id": str(uuid.uuid4()),
            "run_id": self._session.run_id,
            "attempt_id": self._session.attempt_id,
            "sequence": self._sequence,
            "type": event_type,
            "node": node,
            "payload": payload,
            "timestamp": utc_now(),
        }


ProviderBuilder = Callable[[ProviderName], ModelProvider]


class GraphManager:
    def __init__(
        self,
        *,
        settings: Settings,
        catalog: CaseCatalog,
        registry: RunRegistry,
        checkpointer: AsyncSqliteSaver,
        fixture_responses: dict[str, Any],
        provider_builder: ProviderBuilder | None = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.registry = registry
        self.checkpointer = checkpointer
        self.optimized_graph = build_optimized_graph(checkpointer)
        self.baseline_graph = build_baseline_graph()
        self._fixture_responses = fixture_responses
        self._provider_builder = provider_builder

    def _provider(self, name: ProviderName) -> ModelProvider:
        if self._provider_builder:
            return self._provider_builder(name)
        if name == "fixture":
            return FixtureReplayProvider(
                self._fixture_responses,
                delay_seconds=self.settings.fixture_delay_seconds,
                max_tokens=self.settings.model_max_tokens,
            )
        if not self.settings.dashscope_api_key:
            raise ProviderError("dashscope_credentials_missing")
        return DashScopeProvider(
            api_key=self.settings.dashscope_api_key,
            base_url=self.settings.dashscope_base_url,
            model=self.settings.dashscope_model,
            timeout_seconds=self.settings.request_timeout_seconds,
            max_tokens=self.settings.model_max_tokens,
        )

    async def prepare_run(self, request: RunRequest) -> RunSession:
        case = self.catalog.get_for_execution(request.case_id)
        self._provider(request.provider)  # fail before the HTTP stream is opened
        run_id = f"run-{uuid.uuid4()}"
        session = RunSession(
            run_id=run_id,
            thread_id=run_id,
            attempt_id=f"attempt-{uuid.uuid4()}",
            case_id=request.case_id,
            profile=request.profile,
            provider_name=request.provider,
            run_kind=request.run_kind,
            overlay_id=request.overlay_id,
            fault_type=request.fault_type,
            case=case,
        )
        await self.registry.create(session)
        return session

    async def prepare_resume(self, run_id: str) -> RunSession:
        record = await self.registry.get(run_id)
        if record["profile"] != "optimized":
            raise InvalidRunStateError("baseline_runs_are_not_resumable")
        if record["status"] not in {"failed", "interrupted"}:
            raise InvalidRunStateError("run_is_not_resumable")
        provider_name = record["provider"]
        self._provider(provider_name)
        snapshot = await self.optimized_graph.aget_state(
            {"configurable": {"thread_id": record["thread_id"]}}
        )
        checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id")
        attempt_id = f"attempt-{uuid.uuid4()}"
        await self.registry.start_resume(run_id, attempt_id)
        return RunSession(
            run_id=run_id,
            thread_id=record["thread_id"],
            attempt_id=attempt_id,
            case_id=record["case_id"],
            profile=record["profile"],
            provider_name=provider_name,
            run_kind=record["run_kind"],
            overlay_id=record["overlay_id"],
            fault_type=record.get("fault_type"),
            case=self.catalog.get_for_execution(record["case_id"]),
            resume=True,
            checkpoint_id=checkpoint_id,
        )

    async def stream(self, session: RunSession) -> AsyncIterator[dict[str, Any]]:
        envelope = EventEnvelopeFactory(session)
        provider = self._provider(session.provider_name)  # one provider instance per attempt
        context = GraphContext(
            provider=provider,
            profile=session.profile,
            failure_injector=NoopFailureInjector(),
            process_instance_id=session.attempt_id,
            checkpoint_id=session.checkpoint_id,
            overlay_id=session.overlay_id,
            fault_type=session.fault_type,
            execution_mode=(
                "live"
                if session.provider_name == "dashscope"
                else ("replay" if session.run_kind == "fault_injection" else "fixture")
            ),
        )
        graph = self.optimized_graph if session.profile == "optimized" else self.baseline_graph
        config = {
            "configurable": {"thread_id": session.thread_id},
            "recursion_limit": 32,
        }
        graph_input = None
        if not session.resume:
            graph_input = initial_state(
                run_id=session.run_id,
                thread_id=session.thread_id,
                attempt_id=session.attempt_id,
                case=session.case,
                run_kind=session.run_kind,
                overlay_id=session.overlay_id,
                fault_type=session.fault_type,
            )
        await self.registry.mark(session.run_id, session.attempt_id, "running")
        yield envelope.create(
            "run_resumed" if session.resume else "run_started",
            "graph",
            {
                "profile": session.profile,
                "provider": session.provider_name,
                "run_kind": session.run_kind,
            },
        )

        last_state: dict[str, Any] | None = None
        execution_started = time.perf_counter()
        try:
            async for mode, chunk in graph.astream(
                graph_input,
                config=config,
                context=context,
                stream_mode=["custom", "values"],
            ):
                if mode == "custom":
                    yield envelope.create(
                        chunk.get("type", "graph_event"),
                        chunk.get("node", "graph"),
                        chunk.get("payload", {}),
                    )
                elif mode == "values":
                    last_state = chunk
            if last_state is None and session.profile == "optimized":
                snapshot = await graph.aget_state(config)
                last_state = dict(snapshot.values)
            last_state = last_state or {}
            last_state.setdefault("metrics", {})["end_to_end_latency_ms"] = round(
                (time.perf_counter() - execution_started) * 1000, 3
            )
            await self._annotate_fault_snapshot(session, last_state)
            status = str(last_state.get("status", "completed"))
            await self.registry.mark(
                session.run_id,
                session.attempt_id,
                "completed",
                state=last_state,
            )
            benchmark = self._benchmark_snapshot(
                {
                    "run_id": session.run_id,
                    "profile": session.profile,
                    "provider": session.provider_name,
                    "run_kind": session.run_kind,
                },
                last_state,
            )
            yield envelope.create(
                "run_finished",
                "graph",
                {
                    "status": status,
                    "report": last_state.get("diagnosis", {}).get("report"),
                    "benchmark": benchmark,
                },
            )
        except asyncio.CancelledError:
            await self.registry.mark(
                session.run_id,
                session.attempt_id,
                "interrupted",
                error="client_stream_interrupted",
                state=last_state,
            )
            raise
        except Exception as exc:
            error = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            await self.registry.mark(
                session.run_id,
                session.attempt_id,
                "failed",
                error=error,
                state=last_state,
            )
            yield envelope.create("run_failed", "graph", {"error": error})

    async def _annotate_fault_snapshot(
        self, session: RunSession, state: dict[str, Any]
    ) -> None:
        record = await self.registry.get(session.run_id)
        attempt_count = len(record["attempts"])
        trajectory = state.setdefault("trajectory", [])
        trajectory.sort(key=lambda item: item.get("event_key", ""))
        fault_triggered = any(
            bool(item.get("detail", {}).get("fault_injected")) for item in trajectory
        )
        processes_by_operation: dict[str, set[str]] = {}
        for item in trajectory:
            detail = item.get("detail", {})
            if detail.get("event_type") != "tool_attempt":
                continue
            operation_id = detail.get("operation_id")
            process_instance = detail.get("process_instance_id")
            if operation_id and process_instance:
                processes_by_operation.setdefault(operation_id, set()).add(process_instance)
        successful_nodes_reexecuted = sum(
            max(0, len(processes) - 1) for processes in processes_by_operation.values()
        )
        metrics = state.setdefault("metrics", {})
        metrics["run_kind"] = session.run_kind
        metrics["fault"] = {
            "overlay_id": session.overlay_id,
            "triggered": fault_triggered,
            "manual_intervention": False,
            "attempt_count": attempt_count,
            "resumed_from_checkpoint": session.resume,
            "successful_nodes_reexecuted": successful_nodes_reexecuted,
        }

    async def get_run(self, run_id: str) -> dict[str, Any]:
        record = await self.registry.get(run_id)
        state = record.get("state")
        record["benchmark"] = self._benchmark_snapshot(record, state or {})
        return record

    def _benchmark_snapshot(
        self, record: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        return serialize_benchmark_snapshot(
            state,
            mode=record["profile"],
            run_kind=record["run_kind"],
            provenance=build_provenance(
                state,
                mode=record["profile"],
                run_kind=record["run_kind"],
                provider_name=record["provider"],
            ),
        )

    async def decide(self, run_id: str, decision: DecisionRequest) -> dict[str, Any]:
        record = await self.registry.get(run_id)
        if decision.selected_cause_id:
            causes = record.get("state", {}).get("diagnosis", {}).get("root_causes", [])
            known = {item.get("cause_id") for item in causes}
            if decision.selected_cause_id not in known:
                raise InvalidRunStateError("selected_cause_id_not_found")
        await self.registry.decide(run_id, decision)
        return await self.get_run(run_id)

    def evaluation_summary(self) -> dict[str, Any]:
        path = self.settings.evaluation_summary_file
        if not path.is_file():
            return {
                "status": "not_run",
                "labels": EVIDENCE_LABELS,
                "message": "No live evaluation summary is available.",
            }
        summary = load_json_object(path)
        summary.setdefault("labels", EVIDENCE_LABELS)
        return summary


@asynccontextmanager
async def open_graph_manager(
    settings: Settings,
    *,
    fixture_responses: dict[str, Any] | None = None,
    provider_builder: ProviderBuilder | None = None,
) -> AsyncIterator[GraphManager]:
    db_path = Path(settings.checkpoint_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_conn = await aiosqlite.connect(str(db_path))
    registry_conn = await aiosqlite.connect(str(db_path))
    registry_conn.row_factory = aiosqlite.Row
    strict_serde = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=None,
    )
    checkpointer = AsyncSqliteSaver(checkpoint_conn, serde=strict_serde)
    registry = RunRegistry(registry_conn)
    await checkpointer.setup()
    await registry.setup()
    catalog = CaseCatalog(settings.cases_file)
    catalog.load()
    fixtures = fixture_responses or load_json_object(settings.fixture_file)
    manager = GraphManager(
        settings=settings,
        catalog=catalog,
        registry=registry,
        checkpointer=checkpointer,
        fixture_responses=fixtures,
        provider_builder=provider_builder,
    )
    try:
        yield manager
    finally:
        await registry_conn.close()
        await checkpoint_conn.close()
