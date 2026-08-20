"""Validate committed synthetic evaluation data without producing results."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .scoring import (
    ALLOWED_TOOLS,
    EvaluationError,
    load_jsonl,
    validate_case_gold_contract,
    validate_overlay_contract,
)


EXPECTED_CATEGORIES = {
    "power_conducted",
    "common_impedance_ground",
    "shield_termination",
    "clock_harmonic",
    "interface_filtering",
    "near_field_coupling",
}
FORBIDDEN_CASE_KEYS = {
    "root_cause",
    "root_cause_id",
    "acceptable_root_cause_ids",
    "gold",
    "answer",
    "expected_answer",
}
# These fields remain available to the runtime/tool adapter, but must never be
# included in the initial Planner/Diagnosis model projection.
MODEL_INPUT_EXCLUDED_KEYS = {
    "case_id",
    "split",
    "category",
    "required_checks",
    "observations",
    "interventions",
    "tool_data",
}


def _case_ids(records: Sequence[Mapping[str, Any]], label: str) -> list[str]:
    case_ids = [record.get("case_id") for record in records]
    if any(not isinstance(case_id, str) or not case_id for case_id in case_ids):
        raise EvaluationError(f"{label}: every record needs a string case_id")
    if len(case_ids) != len(set(case_ids)):
        raise EvaluationError(f"{label}: duplicate case_id")
    return case_ids  # type: ignore[return-value]


def _find_forbidden_keys(
    value: Any, forbidden: set[str], location: str = "$"
) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            child_location = f"{location}.{key}"
            if key in forbidden:
                found.append(child_location)
            found.extend(_find_forbidden_keys(nested, forbidden, child_location))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(
                _find_forbidden_keys(nested, forbidden, f"{location}[{index}]")
            )
    return found


def project_initial_model_input(case: Mapping[str, Any]) -> dict[str, Any]:
    """Reference projection; backend must enforce the same exclusion boundary."""

    return {
        key: value
        for key, value in case.items()
        if key not in MODEL_INPUT_EXCLUDED_KEYS
    }


def _validate_split(
    cases: Sequence[Mapping[str, Any]],
    gold_records: Sequence[Mapping[str, Any]],
    label: str,
) -> tuple[int, int]:
    gold_by_id = {str(record["case_id"]): record for record in gold_records}
    verified_tags = 0
    executable_sources = 0
    for index, case in enumerate(cases):
        case_id = str(case["case_id"])
        gold = gold_by_id[case_id]
        validate_case_gold_contract(case, gold, f"{label}[{index}]")
        verified_tags += len(gold["required_evidence_tags"])
        tools = [str(item.get("tool", "")) for item in case.get("tool_data", [])]
        if set(tools) != set(ALLOWED_TOOLS) or len(tools) != len(ALLOWED_TOOLS):
            raise EvaluationError(
                f"{case_id}: committed blind benchmark requires one source for every canonical tool"
            )
        executable_sources += len(tools)
        projected = project_initial_model_input(case)
        leaks = _find_forbidden_keys(
            projected,
            FORBIDDEN_CASE_KEYS
            | MODEL_INPUT_EXCLUDED_KEYS
            | {"evidence_tags", "source_id"},
        )
        if leaks:
            raise EvaluationError(
                f"{case_id}: initial model projection leaks evaluator/tool fields: {leaks}"
            )
    return verified_tags, executable_sources


def validate_data(base_dir: Path | None = None) -> dict[str, Any]:
    root = base_dir or Path(__file__).resolve().parent / "data"
    dev_cases = load_jsonl(root / "dev" / "cases.jsonl")
    dev_gold = load_jsonl(root / "dev" / "gold.jsonl")
    frozen_cases = load_jsonl(root / "frozen" / "cases.jsonl")
    frozen_gold = load_jsonl(root / "frozen" / "gold.jsonl")
    overlays = load_jsonl(root / "faults" / "overlays.jsonl")

    if len(dev_cases) != 6 or len(dev_gold) != 6:
        raise EvaluationError("development data must contain exactly 6 case/gold rows")
    if len(frozen_cases) != 24 or len(frozen_gold) != 24:
        raise EvaluationError("frozen data must contain exactly 24 case/gold rows")
    if len(overlays) != 12:
        raise EvaluationError("fault data must contain exactly 12 overlays")

    dev_case_ids = _case_ids(dev_cases, "dev cases")
    dev_gold_ids = _case_ids(dev_gold, "dev gold")
    frozen_case_ids = _case_ids(frozen_cases, "frozen cases")
    frozen_gold_ids = _case_ids(frozen_gold, "frozen gold")
    if set(dev_case_ids) != set(dev_gold_ids):
        raise EvaluationError("development case/gold foreign keys do not match")
    if set(frozen_case_ids) != set(frozen_gold_ids):
        raise EvaluationError("frozen case/gold foreign keys do not match")
    if set(dev_case_ids) & set(frozen_case_ids):
        raise EvaluationError("development and frozen case IDs must be disjoint")

    leaks = {
        str(record["case_id"]): _find_forbidden_keys(record, FORBIDDEN_CASE_KEYS)
        for record in [*dev_cases, *frozen_cases]
        if _find_forbidden_keys(record, FORBIDDEN_CASE_KEYS)
    }
    if leaks:
        raise EvaluationError(f"answer-like keys leaked into runtime cases: {leaks}")

    dev_categories = Counter(record.get("category") for record in dev_cases)
    frozen_categories = Counter(record.get("category") for record in frozen_cases)
    if set(dev_categories) != EXPECTED_CATEGORIES or any(
        count != 1 for count in dev_categories.values()
    ):
        raise EvaluationError("development data must contain one case per category")
    if set(frozen_categories) != EXPECTED_CATEGORIES or any(
        count != 4 for count in frozen_categories.values()
    ):
        raise EvaluationError("frozen data must contain four cases per category")

    root_cause_counts = Counter(record.get("root_cause_id") for record in frozen_gold)
    if len(root_cause_counts) != 6 or any(
        count != 4 for count in root_cause_counts.values()
    ):
        raise EvaluationError("frozen gold must contain six root-cause classes x four")

    dev_tags, dev_sources = _validate_split(dev_cases, dev_gold, "dev")
    frozen_tags, frozen_sources = _validate_split(frozen_cases, frozen_gold, "frozen")
    verified_tags = dev_tags + frozen_tags
    executable_sources = dev_sources + frozen_sources

    frozen_gold_by_id = {str(record["case_id"]): record for record in frozen_gold}
    category_roots: dict[str, set[str]] = defaultdict(set)
    for case in frozen_cases:
        category_roots[str(case["category"])].add(
            str(frozen_gold_by_id[str(case["case_id"])]["root_cause_id"])
        )
    semantic_warnings: list[str] = []
    if all(len(roots) == 1 for roots in category_roots.values()):
        semantic_warnings.append(
            "category deterministically identifies a gold class in this synthetic set; "
            "category and the category-bearing case_id prefix are mandatory initial-model exclusions"
        )
    semantic_warnings.append(
        "required_checks, observations, interventions, tool_data, tool values and evidence_tags "
        "are runtime/tool-only and must be revealed only by successful tool results"
    )

    overlay_ids = [record.get("overlay_id") for record in overlays]
    if any(not isinstance(item, str) or not item for item in overlay_ids):
        raise EvaluationError("every fault overlay needs an overlay_id")
    if len(overlay_ids) != len(set(overlay_ids)):
        raise EvaluationError("fault overlay_id values must be unique")
    frozen_by_id = {str(record["case_id"]): record for record in frozen_cases}
    unknown_fault_cases = sorted(
        {
            record.get("case_id")
            for record in overlays
            if record.get("case_id") not in frozen_by_id
        }
    )
    if unknown_fault_cases:
        raise EvaluationError(f"fault overlays refer to unknown cases: {unknown_fault_cases}")
    for index, overlay in enumerate(overlays):
        validate_overlay_contract(
            overlay,
            frozen_by_id[str(overlay["case_id"])],
            f"fault_overlays[{index}]",
        )
    fault_types = Counter(record.get("fault_type") for record in overlays)
    expected_fault_types = {
        "transient_tool_error": 4,
        "timeout_once": 4,
        "process_interrupt": 4,
    }
    if dict(fault_types) != expected_fault_types:
        raise EvaluationError(
            f"fault overlays must have 4/4/4 type balance, got {dict(fault_types)}"
        )

    return {
        "dev_cases": len(dev_cases),
        "frozen_cases": len(frozen_cases),
        "categories": dict(sorted(frozen_categories.items())),
        "root_cause_classes": dict(sorted(root_cause_counts.items())),
        "fault_types": dict(sorted(fault_types.items())),
        "gold_tag_sources_verified": verified_tags,
        "executable_tool_sources_verified": executable_sources,
        "runtime_answer_key_leaks": 0,
        "model_input_projection_excludes": sorted(MODEL_INPUT_EXCLUDED_KEYS),
        "semantic_leakage_warnings": semantic_warnings,
    }


def main() -> int:
    import json

    print(json.dumps(validate_data(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
