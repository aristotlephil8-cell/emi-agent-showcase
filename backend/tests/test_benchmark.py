from __future__ import annotations

import pytest

from app.benchmark import run_to_completion

from .helpers import CASE, OverlapProbe, RecordingProvider, standard_responses


@pytest.mark.asyncio
async def test_run_to_completion_returns_jsonl_ready_contract() -> None:
    snapshot = await run_to_completion(
        CASE,
        "optimized",
        RecordingProvider(standard_responses()),
    )
    assert snapshot["mode"] == "optimized"
    assert snapshot["run_kind"] == "evaluation"
    assert snapshot["status"] == "completed"
    assert "FIXTURE_REPLAY_NOT_LIVE" in snapshot["labels"]
    assert "LIVE_SYNTHETIC_SINGLE_RUN" not in snapshot["labels"]
    assert snapshot["plan"]["steps"][0]["depends_on"] == []
    assert snapshot["evidence"][0]["phase"] == "initial"
    assert snapshot["evidence"][0]["status"] == "success"
    assert snapshot["diagnosis"]["candidates"][0] == {
        "root_cause_id": "clock_harmonic_radiation",
        "rank": 1,
        "label": "synthetic cause",
        "confidence": 0.8,
        "evidence_ids": snapshot["diagnosis"]["candidates"][0]["evidence_ids"],
        "rationale": "both measurements were verified",
    }
    assert "initial_diagnosis" in snapshot["review"]
    assert set(snapshot["diagnosis"]) == {"candidates", "claims"}
    assert "contradicting_evidence_ids" in snapshot["diagnosis"]["claims"][0]
    assert "latency_ms" in snapshot["metrics"]
    assert snapshot["metrics"]["inference_config"] == {
        "temperature": 0,
        "enable_thinking": False,
        "max_tokens": 2048,
        "requested_model": "fixture-replay-v1",
    }
    assert set(snapshot["provenance"]) == {
        "provider",
        "execution_mode",
        "model",
        "requested_model",
        "actual_model",
        "fallback_reason",
        "config_hash",
        "prompt_hashes",
        "data_hash",
    }
    assert snapshot["provenance"]["execution_mode"] == "fixture"
    assert snapshot["provenance"]["provider"] == "fixture"
    tool_proof = next(
        item["detail"]
        for item in snapshot["trajectory"]
        if item.get("detail", {}).get("event_type") == "tool_attempt"
    )
    assert set(tool_proof) >= {
        "fault_injected",
        "tool_attempt",
        "process_instance_id",
        "checkpoint_id",
        "operation_id",
    }


@pytest.mark.asyncio
async def test_parallel_wall_latency_is_not_summed_node_time() -> None:
    snapshot = await run_to_completion(
        CASE,
        "optimized",
        RecordingProvider(standard_responses()),
        failure_injector=OverlapProbe(0.06),
    )
    assert snapshot["metrics"]["latency_ms"] == snapshot["metrics"][
        "end_to_end_latency_ms"
    ]
    assert snapshot["metrics"]["latency_ms"] < snapshot["metrics"]["node_time_ms"]


@pytest.mark.asyncio
async def test_evaluation_case_adapter_strips_category_and_split_labels() -> None:
    class CaseSpyProvider(RecordingProvider):
        planner_case = None
        evidence_inputs = []

        async def complete(self, **kwargs):  # type: ignore[no-untyped-def]
            if kwargs["role"] == "planner":
                self.planner_case = kwargs["input_data"]["case"]
            if kwargs["role"] == "evidence":
                self.evidence_inputs.append(kwargs["input_data"])
            return await super().complete(**kwargs)

    provider = CaseSpyProvider(standard_responses())
    frozen_case = {
        **CASE,
        "schema_version": "evaluation-v1",
        "split": "evaluation",
        "category": "label-must-not-reach-model",
        "context": {
            "operating_mode": "synthetic",
            "source_id": "must-not-leak",
            "nested": {"path_id": "must-not-leak", "safe": "visible"},
        },
        "observations": {"a": 1.0, "b": 2.0},
        "constraints": {"synthetic_only": True},
        "required_checks": ["tool_coverage"],
        "measurements": {
            "a": {"value": 1.0, "evidence_tags": ["obs:a"]},
            "b": {"values": [2.0, 2.1, 1.9], "evidence_tags": ["obs:b"]},
        },
        "tool_data": [
            {
                "source_id": "obs-a",
                "tool": "query_measurement",
                "arguments": {"key": "a"},
                "payload": {"value": 1.0},
                "evidence_tags": ["obs:a"],
            },
            {
                "source_id": "obs-b",
                "tool": "check_measurement_consistency",
                "arguments": {"measurement_key": "b", "tolerance_percent": 10},
                "payload": {
                    "measurement_key": "b",
                    "repeat_count": 3,
                    "max_delta_percent": 5.0,
                    "consistent": True,
                },
                "evidence_tags": ["obs:b"],
            },
        ],
    }
    snapshot = await run_to_completion(frozen_case, "optimized", provider)
    assert provider.planner_case is not None
    assert "category" not in provider.planner_case
    assert "split" not in provider.planner_case
    assert "schema_version" not in provider.planner_case
    assert "case_id" not in provider.planner_case
    assert set(provider.planner_case) == {"title", "symptom", "context", "constraints"}

    excluded_keys = {
        "observations",
        "measurements",
        "interventions",
        "coupling_paths",
        "required_checks",
        "tool_data",
        "source_id",
        "evidence_tags",
        "payload",
    }

    def all_keys(value):  # type: ignore[no-untyped-def]
        if isinstance(value, dict):
            return set(value) | {
                key for child in value.values() for key in all_keys(child)
            }
        if isinstance(value, list):
            return {key for child in value for key in all_keys(child)}
        return set()

    assert not (all_keys(provider.planner_case) & excluded_keys)
    assert provider.evidence_inputs
    assert all(item["step"]["arguments"] == {} for item in provider.evidence_inputs)
    assert all(
        not (all_keys(item["case"]) & excluded_keys)
        for item in provider.evidence_inputs
    )
    assert snapshot["plan"]["steps"][0]["arguments"] == {"key": "a"}
    tags = {tag for item in snapshot["evidence"] for tag in item["evidence_tags"]}
    assert tags == {"obs:a", "obs:b"}
