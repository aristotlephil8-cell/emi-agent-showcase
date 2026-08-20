from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.evaluation_runner import (
    FaultScenario,
    load_fault_scenarios,
    load_recorded_responses,
    run_fault_replays,
    run_interleaved,
    run_one,
)

from .helpers import CASE, RecordingProvider, standard_responses


class DashScopeStub(RecordingProvider):
    """Network-free class name probe for recording-wrapper provenance routing."""


class FailingFixtureProvider:
    provider_name = "FixtureReplayProvider"

    async def complete(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        raise RuntimeError("sanitized_fixture_failure")


@pytest.mark.asyncio
async def test_runner_interleaves_variants_and_resumes_without_duplicates() -> None:
    runtime_dir = Path(__file__).resolve().parents[1] / ".test-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    output = runtime_dir / f"runs-{uuid.uuid4()}.jsonl"
    recordings = runtime_dir / f"recordings-{uuid.uuid4()}.jsonl"
    responses = standard_responses()
    responses["cases"]["test-case-2"] = responses["cases"]["test-case"]
    second_case = {**CASE, "case_id": "test-case-2"}
    factory = lambda: RecordingProvider(responses)

    first = await run_interleaved(
        [CASE, second_case], factory, output, recording_path=recordings
    )
    second = await run_interleaved([CASE, second_case], factory, output)

    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [item["variant"] for item in first] == [
        "baseline",
        "optimized",
        "optimized",
        "baseline",
    ]
    assert second == []
    assert len(records) == 4
    assert all(item["provenance"]["execution_mode"] == "fixture" for item in records)
    source_run_id = next(
        item["run_id"]
        for item in records
        if item["case_id"] == "test-case" and item["variant"] == "optimized"
    )
    replay = load_recorded_responses(
        recordings, variant="optimized", source_run_id=source_run_id
    )
    assert replay["fixture_kind"] == "RECORDED_STRUCTURED_OUTPUT_REPLAY"
    assert replay["cases"]["test-case"]["planner"]["steps"][0]["step_id"] == "s1"
    recording_records = [
        json.loads(line) for line in recordings.read_text(encoding="utf-8").splitlines()
    ]
    source_records = [
        item for item in recording_records if item["source_run_id"] == source_run_id
    ]
    assert [item["call_sequence"] for item in source_records] == list(
        range(1, len(source_records) + 1)
    )
    diagnosis = next(item for item in source_records if item["role"] == "diagnosis")
    assert diagnosis["data"]["root_causes"][0]["evidence_ids"] == [
        "{{evidence:s1}}",
        "{{evidence:s2}}",
    ]


@pytest.mark.asyncio
async def test_fault_runner_uses_exact_recording_and_resumes_process_interrupt() -> None:
    runtime_dir = Path(__file__).resolve().parents[1] / ".test-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    source_runs = runtime_dir / f"source-runs-{uuid.uuid4()}.jsonl"
    recordings = runtime_dir / f"source-recordings-{uuid.uuid4()}.jsonl"
    fault_runs = runtime_dir / f"fault-runs-{uuid.uuid4()}.jsonl"
    checkpoint_dir = runtime_dir / f"fault-checkpoints-{uuid.uuid4()}"
    await run_interleaved(
        [CASE],
        lambda: RecordingProvider(standard_responses()),
        source_runs,
        recording_path=recordings,
    )
    scenario = FaultScenario(
        case_id="test-case",
        overlay_id="process-interrupt-test",
        fault_type="process_interrupt",
        target_node="diagnosis_agent",
        after_node="evidence_worker",
    )

    snapshots = await run_fault_replays(
        [CASE],
        [scenario],
        recordings,
        source_runs,
        fault_runs,
        checkpoint_dir=checkpoint_dir,
    )

    assert len(snapshots) == 2
    baseline = next(item for item in snapshots if item["variant"] == "baseline")
    optimized = next(item for item in snapshots if item["variant"] == "optimized")
    assert baseline["status"] == "failed"
    assert set(baseline) >= {
        "plan",
        "evidence",
        "diagnosis",
        "review",
        "trajectory",
        "metrics",
        "provenance",
    }
    assert optimized["status"] == "completed"
    assert optimized["fault"]["triggered"] is True
    assert optimized["fault"]["resumed_from_checkpoint"] is True
    assert optimized["fault"]["successful_nodes_reexecuted"] == 0
    assert optimized["provenance"]["source_run_id"]
    proof = next(
        item["detail"]["checkpoint_resume"]
        for item in optimized["trajectory"]
        if item.get("detail", {}).get("event_type") == "checkpoint_resume"
    )
    assert proof["from"]["checkpoint_id"] == proof["to"][
        "resumed_from_checkpoint_id"
    ]
    assert proof["from"]["process_instance_id"] != proof["to"][
        "process_instance_id"
    ]


def test_fault_manifest_requires_runtime_tool_selector() -> None:
    runtime_dir = Path(__file__).resolve().parents[1] / ".test-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / f"faults-{uuid.uuid4()}.json"
    path.write_text(
        json.dumps(
            {
                "overlays": [
                    {
                        "overlay_id": "retry-1",
                        "case_id": "test-case",
                        "fault_type": "transient_tool_error",
                        "target": {
                            "node": "evidence_worker",
                            "tool": "query_measurement",
                            "selector": "first_matching_tool",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    scenarios = load_fault_scenarios(path)

    assert scenarios == [
        FaultScenario(
            case_id="test-case",
            overlay_id="retry-1",
            fault_type="transient_tool_error",
            tool="query_measurement",
            selector="first_matching_tool",
        )
    ]


@pytest.mark.asyncio
async def test_recording_wrapper_preserves_live_provenance_classification() -> None:
    runtime_dir = Path(__file__).resolve().parents[1] / ".test-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    output = runtime_dir / f"live-wrapper-{uuid.uuid4()}.jsonl"
    recordings = runtime_dir / f"live-wrapper-recording-{uuid.uuid4()}.jsonl"

    snapshots = await run_interleaved(
        [CASE],
        lambda: DashScopeStub(standard_responses()),
        output,
        variants=("baseline",),
        recording_path=recordings,
    )

    assert snapshots[0]["provenance"]["provider"] == "dashscope"
    assert snapshots[0]["provenance"]["execution_mode"] == "live"
    assert "LIVE_SYNTHETIC_SINGLE_RUN" in snapshots[0]["labels"]


@pytest.mark.asyncio
async def test_failed_run_keeps_attempted_model_call_cost_and_full_contract() -> None:
    snapshot = await run_one(CASE, "optimized", FailingFixtureProvider())

    assert snapshot["status"] == "failed"
    assert snapshot["metrics"]["model_calls"] == 1
    assert snapshot["metrics"]["latency_ms"] > 0
    assert snapshot["plan"] == {"hypotheses": [], "steps": []}
    assert snapshot["diagnosis"] == {"candidates": [], "claims": []}
    assert snapshot["review"] == {
        "initial_diagnosis": {"candidates": [], "claims": []},
        "issues": [],
    }
