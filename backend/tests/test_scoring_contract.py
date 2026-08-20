from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.benchmark import run_to_completion
from app.evaluation_runner import (
    load_fault_scenarios,
    run_fault_replays,
    run_interleaved,
)
from app.providers import FixtureReplayProvider


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from evaluation.scoring import score_fault_run, score_run  # noqa: E402


def _jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _fixture_responses(case: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    steps = [
        {
            "step_id": f"step-{index}",
            "hypothesis_id": "h1",
            "tool": item["tool"],
            "arguments": item["arguments"],
            "depends_on": [],
            "completion_condition": f"collect {item['source_id']}",
        }
        for index, item in enumerate(case["tool_data"], start=1)
    ]
    evidence_placeholders = [
        f"{{{{evidence:{step['step_id']}}}}}" for step in steps
    ]
    return {
        "cases": {
            case["case_id"]: {
                "planner": {
                    "hypotheses": [
                        {
                            "hypothesis_id": "h1",
                            "statement": "frozen synthetic EMI hypothesis",
                            "rationale": "all declared runtime checks are required",
                        }
                    ],
                    "information_gaps": ["collect declared tool evidence"],
                    "steps": steps,
                },
                "diagnosis": {
                    "root_causes": [
                        {
                            "cause_id": gold["root_cause_id"],
                            "label": "frozen synthetic candidate",
                            "confidence": 0.85,
                            "evidence_ids": evidence_placeholders,
                            "rationale": "all frozen runtime checks returned evidence",
                        }
                    ],
                    "claims": [
                        {
                            "claim_id": "claim-1",
                            "text": "the candidate is supported by the declared checks",
                            "evidence_ids": evidence_placeholders,
                            "contradicted_by": [],
                            "support_status": "supported",
                        }
                    ],
                    "confidence_boundary": "synthetic fixture integration test only",
                    "status": "complete",
                },
                "reviewer": {"issues": [], "needs_rework": False},
            }
        }
    }


@pytest.mark.asyncio
async def test_backend_snapshot_is_consumed_by_real_evaluation_scorer() -> None:
    case = _jsonl(REPOSITORY_ROOT / "evaluation/data/frozen/cases.jsonl")[0]
    gold = _jsonl(REPOSITORY_ROOT / "evaluation/data/frozen/gold.jsonl")[0]
    responses = _fixture_responses(case, gold)

    snapshot = await run_to_completion(
        case,
        "optimized",
        FixtureReplayProvider(responses),
    )
    score = score_run(snapshot, case, gold)

    assert score["plan"]["executable"] is True
    assert score["task_completed"] is True
    assert score["top1_root_cause_id"] == gold["root_cause_id"]
    assert score["top1_hit"] is True
    assert score["bad_claim_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overlay_id", ["FAULT-009", "FAULT-010", "FAULT-011", "FAULT-012"]
)
async def test_process_resume_trajectory_passes_real_fault_scorer(
    overlay_id: str,
) -> None:
    cases = _jsonl(REPOSITORY_ROOT / "evaluation/data/frozen/cases.jsonl")
    gold_records = _jsonl(REPOSITORY_ROOT / "evaluation/data/frozen/gold.jsonl")
    overlays_path = REPOSITORY_ROOT / "evaluation/data/faults/overlays.jsonl"
    overlays = _jsonl(overlays_path)
    overlay = next(item for item in overlays if item["overlay_id"] == overlay_id)
    case = next(item for item in cases if item["case_id"] == overlay["case_id"])
    gold = next(item for item in gold_records if item["case_id"] == overlay["case_id"])
    scenario = next(
        item
        for item in load_fault_scenarios(overlays_path)
        if item.overlay_id == overlay["overlay_id"]
    )
    responses = _fixture_responses(case, gold)
    runtime_dir = Path(__file__).resolve().parents[1] / ".test-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    source_runs = runtime_dir / f"scorer-source-{uuid.uuid4()}.jsonl"
    recordings = runtime_dir / f"scorer-recordings-{uuid.uuid4()}.jsonl"
    fault_runs = runtime_dir / f"scorer-fault-{uuid.uuid4()}.jsonl"
    await run_interleaved(
        [case],
        lambda: FixtureReplayProvider(responses),
        source_runs,
        recording_path=recordings,
    )
    snapshots = await run_fault_replays(
        [case],
        [scenario],
        recordings,
        source_runs,
        fault_runs,
        checkpoint_dir=runtime_dir / f"scorer-checkpoints-{uuid.uuid4()}",
    )
    optimized = next(item for item in snapshots if item["variant"] == "optimized")

    score = score_fault_run(optimized, overlay, case, gold)

    assert score["fault_triggered"] is True
    assert score["proof"]["from_checkpoint_id"] == score["proof"][
        "resumed_from_checkpoint_id"
    ]
    assert score["proof"]["reexecuted_successful_node_keys"] == []
    assert score["recovered"] is True
