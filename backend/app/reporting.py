from __future__ import annotations

from typing import Any


BASE_EVIDENCE_LABELS = [
    "DEVELOPMENT_V1",
    "NOT_EXPERT_VALIDATED",
]
EVIDENCE_LABELS = BASE_EVIDENCE_LABELS


def labels_for_execution(execution_mode: str) -> list[str]:
    run_label = (
        "LIVE_SYNTHETIC_SINGLE_RUN"
        if execution_mode == "live"
        else "FIXTURE_REPLAY_NOT_LIVE"
    )
    return [BASE_EVIDENCE_LABELS[0], run_label, BASE_EVIDENCE_LABELS[1]]


def build_report(
    state: dict[str, Any], *, execution_mode: str = "fixture"
) -> tuple[dict[str, Any], str]:
    """Format graph outputs without an additional model or fictional agent."""

    diagnosis = state.get("diagnosis", {})
    review = state.get("review", {})
    unresolved_high = [
        issue
        for issue in review.get("issues", [])
        if issue.get("severity") == "high" and not issue.get("resolved", False)
    ]
    failed_evidence = [
        record for record in state.get("evidence", []) if record.get("status") != "success"
    ]
    status = "completed"
    if (
        diagnosis.get("status") != "complete"
        or unresolved_high
        or failed_evidence
    ):
        status = "needs_human_review"

    safe_execution_mode = (
        execution_mode if execution_mode in {"live", "fixture", "replay"} else "fixture"
    )
    report = {
        "labels": labels_for_execution(safe_execution_mode),
        "run_id": state.get("run_id"),
        "case_id": state.get("case", {}).get("case_id"),
        "status": status,
        "ranked_root_causes": diagnosis.get("root_causes", []),
        "atomic_claims": diagnosis.get("claims", []),
        "confidence_boundary": diagnosis.get("confidence_boundary", ""),
        "evidence_ledger": sorted(
            state.get("evidence", []), key=lambda item: item.get("operation_id", "")
        ),
        "review": review,
        "human_decision_required": True,
        "metrics": state.get("metrics", {}),
    }
    return report, status
