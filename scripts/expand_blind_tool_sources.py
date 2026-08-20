"""Add neutral executable sources for every canonical tool in synthetic cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


TOOL_ORDER = (
    "query_measurement",
    "match_frequency_signature",
    "compare_intervention",
    "inspect_coupling_path",
    "check_measurement_consistency",
)


def _neutral_source(tool: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    if tool == "query_measurement":
        source_id = "neutral-query"
        return (
            {"id": source_id, "kind": "auxiliary_measurement", "value": 0.0},
            "observations",
            {"source_id": source_id, "tool": tool, "arguments": {"key": source_id}, "payload": {"measurement": {"id": source_id, "value": 0.0}}, "evidence_tags": ["neutral_query_measurement"]},
        )
    if tool == "match_frequency_signature":
        source_id = "neutral-frequency"
        return (
            {"id": source_id, "kind": "auxiliary_spectrum", "frequency_mhz": 1.0},
            "observations",
            {"source_id": source_id, "tool": tool, "arguments": {"fundamental_mhz": 1.0, "peak_key": source_id, "tolerance_mhz": 0.1}, "payload": {"measurement_ref": source_id, "fundamental_mhz": 1.0, "harmonic_order": None, "delta_mhz": 1.0, "matched": False}, "evidence_tags": ["neutral_frequency_signature"]},
        )
    if tool == "compare_intervention":
        source_id = "neutral-intervention"
        return (
            {"id": source_id, "change": "辅助对照未改变被测对象", "before_dbuv": 50.0, "after_dbuv": 50.0},
            "interventions",
            {"source_id": source_id, "tool": tool, "arguments": {"intervention_id": source_id}, "payload": {"intervention_id": source_id, "before_dbuv": 50.0, "after_dbuv": 50.0, "delta_db": 0.0}, "evidence_tags": ["neutral_intervention_comparison"]},
        )
    if tool == "inspect_coupling_path":
        source_id = "neutral-path-source"
        return (
            {"id": source_id, "kind": "auxiliary_path_probe"},
            "observations",
            {"source_id": source_id, "tool": tool, "arguments": {"path_id": "neutral-path"}, "payload": {"path_id": "neutral-path", "source_ref": source_id, "victim_ref": source_id, "path_status": "not_traceable", "plausible": False, "observation": "auxiliary path has no direct coupling evidence"}, "evidence_tags": ["neutral_coupling_path"]},
        )
    if tool == "check_measurement_consistency":
        source_id = "neutral-repeat"
        return (
            {"id": source_id, "kind": "auxiliary_repeat_measurement", "value": 0.0},
            "observations",
            {"source_id": source_id, "tool": tool, "arguments": {"measurement_key": source_id, "tolerance_percent": 10}, "payload": {"measurement_key": source_id, "consistent": True, "max_delta_percent": 0.0}, "evidence_tags": ["neutral_measurement_consistency"]},
        )
    raise ValueError(f"unsupported tool: {tool}")


def _expand_case(case: dict[str, Any]) -> bool:
    tool_data = case.setdefault("tool_data", [])
    present_tools = {item.get("tool") for item in tool_data if isinstance(item, dict)}
    source_ids = {item.get("id") for collection in ("observations", "interventions") for item in case.get(collection, []) if isinstance(item, dict)}
    tags = {tag for item in tool_data if isinstance(item, dict) for tag in item.get("evidence_tags", [])}
    changed = False
    for tool in TOOL_ORDER:
        if tool in present_tools:
            continue
        source, collection, item = _neutral_source(tool)
        source_id = source["id"]
        if source_id in source_ids or tags.intersection(item["evidence_tags"]):
            raise ValueError(f"{case['case_id']}: neutral source or tag collision")
        case.setdefault(collection, []).append(source)
        tool_data.append(item)
        source_ids.add(source_id)
        tags.update(item["evidence_tags"])
        changed = True
    case["tool_data"] = sorted(tool_data, key=lambda item: TOOL_ORDER.index(item["tool"]))
    return changed


def _rewrite(path: Path) -> int:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    changed = sum(_expand_case(case) for case in cases)
    path.write_text("\n".join(json.dumps(case, ensure_ascii=False, separators=(",", ":")) for case in cases) + "\n", encoding="utf-8")
    return changed


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "evaluation" / "data"
    print(json.dumps({"neutral_source_expansion": {split: _rewrite(root / split / "cases.jsonl") for split in ("dev", "frozen")}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
