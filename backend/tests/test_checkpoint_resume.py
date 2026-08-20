from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import aiosqlite
import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.graph import (
    GraphContext,
    InjectedRunCrash,
    NoopFailureInjector,
    build_optimized_graph,
    initial_state,
)
from app.benchmark import serialize_benchmark_snapshot

from .helpers import CASE, RecordingProvider, standard_responses


class CrashSecondWorker:
    async def before_tool(self, **context) -> None:  # type: ignore[no-untyped-def]
        if context["step_id"] == "s2":
            await asyncio.sleep(0.08)
            raise InjectedRunCrash("simulated_process_interruption")


def strict_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=None,
    )


@pytest.mark.asyncio
async def test_new_graph_instance_resumes_without_rerunning_successful_worker() -> None:
    runtime_dir = Path(__file__).resolve().parents[1] / ".test-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    db_path = runtime_dir / f"resume-{uuid.uuid4()}.sqlite"
    config = {"configurable": {"thread_id": "resume-thread"}}

    first_conn = await aiosqlite.connect(db_path)
    first_saver = AsyncSqliteSaver(first_conn, serde=strict_serde())
    await first_saver.setup()
    first_graph = build_optimized_graph(first_saver)
    first_provider = RecordingProvider(standard_responses())
    state = initial_state(
        run_id="resume-run",
        thread_id="resume-thread",
        attempt_id="process-1",
        case=CASE,
        run_kind="fault_injection",
        overlay_id="process-interruption",
    )
    with pytest.raises(InjectedRunCrash):
        async for _ in first_graph.astream(
            state,
            config=config,
            context=GraphContext(
                provider=first_provider,
                profile="optimized",
                failure_injector=CrashSecondWorker(),
                process_instance_id="process-1",
            ),
            stream_mode=["custom", "values"],
        ):
            pass
    interrupted_snapshot = await first_graph.aget_state(config)
    interrupted_checkpoint_id = interrupted_snapshot.config["configurable"]["checkpoint_id"]
    await first_conn.close()
    assert "s1" in first_provider.evidence_steps

    second_conn = await aiosqlite.connect(db_path)
    second_saver = AsyncSqliteSaver(second_conn, serde=strict_serde())
    await second_saver.setup()
    second_graph = build_optimized_graph(second_saver)
    second_provider = RecordingProvider(standard_responses())
    final_state = {}
    async for mode, chunk in second_graph.astream(
        None,
        config=config,
        context=GraphContext(
            provider=second_provider,
            profile="optimized",
            failure_injector=NoopFailureInjector(),
            process_instance_id="process-2",
            checkpoint_id=interrupted_checkpoint_id,
        ),
        stream_mode=["custom", "values"],
    ):
        if mode == "values":
            final_state = chunk
    await second_conn.close()

    assert second_provider.evidence_steps == ["s2"]
    assert final_state["status"] == "completed"
    assert {item["step_id"] for item in final_state["evidence"]} == {"s1", "s2"}
    snapshot = serialize_benchmark_snapshot(
        final_state, mode="optimized", run_kind="fault_injection"
    )
    assert snapshot["fault"]["triggered"] is True
    assert snapshot["fault"]["resumed_from_checkpoint"] is True
    assert snapshot["fault"]["successful_nodes_reexecuted"] == 0
    resume_event = next(
        item
        for item in snapshot["trajectory"]
        if item.get("detail", {}).get("event_type") == "checkpoint_resume"
    )
    proof = resume_event["detail"]["checkpoint_resume"]
    assert proof["from"]["checkpoint_id"] == interrupted_checkpoint_id
    assert proof["to"]["resumed_from_checkpoint_id"] == interrupted_checkpoint_id
    assert proof["from"]["process_instance_id"] != proof["to"]["process_instance_id"]
