from __future__ import annotations

import asyncio
from typing import Any

from app.providers import FixtureReplayProvider, ProviderResponse


CASE = {
    "case_id": "test-case",
    "title": "synthetic test case",
    "category": "test",
    "symptom": "two measurements require parallel verification",
    "context": {},
    "measurements": {"a": 1.0, "b": [2.0, 2.1, 1.9]},
    "interventions": [],
    "coupling_paths": [],
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


def plan_response() -> dict[str, Any]:
    return {
        "hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "synthetic hypothesis",
                "rationale": "two independent measurements are available",
            }
        ],
        "information_gaps": ["measurement a", "measurement b"],
        "steps": [
            {
                "step_id": "s1",
                "hypothesis_id": "h1",
                "tool": "query_measurement",
                "arguments": {"key": "a"},
                "depends_on": [],
                "completion_condition": "measurement a is returned",
            },
            {
                "step_id": "s2",
                "hypothesis_id": "h1",
                "tool": "check_measurement_consistency",
                "arguments": {"measurement_key": "b", "tolerance_percent": 10},
                "depends_on": [],
                "completion_condition": "measurement b is returned",
            },
        ],
    }


def supported_diagnosis() -> dict[str, Any]:
    return {
        "root_causes": [
            {
                "cause_id": "clock_harmonic_radiation",
                "label": "synthetic cause",
                "confidence": 0.8,
                "evidence_ids": ["{{evidence:s1}}", "{{evidence:s2}}"],
                "rationale": "both measurements were verified",
            }
        ],
        "claims": [
            {
                "claim_id": "c1",
                "text": "both measurements support the candidate",
                "evidence_ids": ["{{evidence:s1}}", "{{evidence:s2}}"],
                "contradicted_by": [],
                "support_status": "supported",
            }
        ],
        "confidence_boundary": "synthetic fixture only",
        "status": "complete",
    }


def standard_responses() -> dict[str, Any]:
    return {
        "cases": {
            "test-case": {
                "planner": plan_response(),
                "diagnosis": supported_diagnosis(),
                "reviewer": {"issues": [], "needs_rework": False},
            }
        }
    }


class RecordingProvider(FixtureReplayProvider):
    def __init__(self, responses: dict[str, Any]):
        super().__init__(responses)
        self.evidence_steps: list[str] = []

    async def complete(
        self,
        *,
        role: str,
        case_id: str,
        input_data: dict[str, Any],
        schema: dict[str, Any],
    ) -> ProviderResponse:
        if role == "evidence":
            self.evidence_steps.append(input_data["step"]["step_id"])
        return await super().complete(
            role=role,
            case_id=case_id,
            input_data=input_data,
            schema=schema,
        )


class OverlapProbe:
    def __init__(self, delay: float = 0.06):
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self._lock = asyncio.Lock()

    async def before_tool(self, **context: Any) -> None:
        del context
        async with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        async with self._lock:
            self.active -= 1
