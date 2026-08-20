from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import BACKEND_ROOT, Settings
from app.main import create_app


def test_sse_emits_first_event_before_final_report_and_persists_snapshot() -> None:
    runtime_dir = BACKEND_ROOT / ".test-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        checkpoint_db=runtime_dir / f"api-{uuid.uuid4()}.sqlite",
        evaluation_summary_file=runtime_dir / "summary-not-created.json",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/v1/runs/stream",
            json={"case_id": "dev-clock-004", "profile": "optimized", "provider": "fixture"},
        ) as response:
            assert response.status_code == 200
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]

        assert events[0]["type"] == "run_started"
        assert events[0]["sequence"] == 1
        assert events[-1]["type"] == "run_finished"
        evidence_event = next(
            event
            for event in events
            if event["type"] == "node_completed" and event["node"] == "evidence_worker"
        )
        diagnosis_event = next(
            event
            for event in events
            if event["type"] == "node_completed" and event["node"] == "diagnosis"
        )
        assert evidence_event["payload"]["record"]["status"] == "success"
        assert diagnosis_event["payload"]["diagnosis"]["root_causes"]
        assert events[-1]["payload"]["benchmark"]["diagnosis"]["candidates"]
        assert events[0]["timestamp"] <= events[-1]["timestamp"]
        run_id = events[0]["run_id"]
        snapshot = client.get(f"/api/v1/runs/{run_id}").json()
        assert snapshot["state"]["status"] == "completed"
        assert snapshot["benchmark"]["plan"]["steps"][0]["depends_on"] == []
        evidence = snapshot["benchmark"]["evidence"][0]
        assert set(evidence) >= {
            "operation_id",
            "step_id",
            "tool",
            "status",
            "phase",
            "supports_claim_ids",
            "contradicts_claim_ids",
            "evidence_id",
            "evidence_tags",
        }
        assert client.get("/api/v1/evaluation/summary").json()["status"] == "not_run"


def test_health_does_not_expose_provider_configuration() -> None:
    runtime_dir = BACKEND_ROOT / ".test-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(checkpoint_db=runtime_dir / f"health-{uuid.uuid4()}.sqlite")
    with TestClient(create_app(settings)) as client:
        body = client.get("/api/v1/health").json()
    assert body["strict_msgpack"] is True
    assert "api_key" not in body
    assert "base_url" not in body


def test_case_catalog_public_view_hides_tool_sources() -> None:
    runtime_dir = BACKEND_ROOT / ".test-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(checkpoint_db=runtime_dir / f"cases-{uuid.uuid4()}.sqlite")
    with TestClient(create_app(settings)) as client:
        cases = client.get("/api/v1/cases").json()

    assert cases
    assert set(cases[0]) == {
        "case_id",
        "title",
        "category",
        "symptom",
        "context",
        "constraints",
    }
