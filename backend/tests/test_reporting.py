from app.reporting import build_report


def test_report_labels_use_explicit_execution_mode_with_safe_default() -> None:
    state = {
        "run_id": "report-test",
        "case": {"case_id": "case-test"},
        "diagnosis": {
            "status": "complete",
            "root_causes": [],
            "claims": [],
            "confidence_boundary": "fixture",
        },
        "review": {"issues": []},
        "evidence": [],
        "metrics": {"models": ["qwen3.7-plus-2026-05-26"]},
    }

    default_report, _ = build_report(state)
    live_report, _ = build_report(state, execution_mode="live")
    invalid_report, _ = build_report(state, execution_mode="unexpected")

    assert "FIXTURE_REPLAY_NOT_LIVE" in default_report["labels"]
    assert "FIXTURE_REPLAY_NOT_LIVE" in invalid_report["labels"]
    assert "LIVE_SYNTHETIC_SINGLE_RUN" in live_report["labels"]
