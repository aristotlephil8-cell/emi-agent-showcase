"""Deterministic scoring and publication gates for EMI-Agent evaluation records."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCORER_VERSION = "2.1.0"
VARIANTS = ("baseline", "optimized")
RUN_KINDS = ("evaluation", "fault_injection")
PUBLISH_LABELS = [
    "DEVELOPMENT_V1",
    "LIVE_SYNTHETIC_SINGLE_RUN",
    "DETERMINISTIC_REPLAY_FAULT_INJECTION",
    "NOT_EXPERT_VALIDATED",
]
PUBLISH_NORMAL_LABELS = [
    "DEVELOPMENT_V1",
    "LIVE_SYNTHETIC_SINGLE_RUN",
    "NOT_EXPERT_VALIDATED",
]
PUBLISH_FAULT_LABELS = [
    "DEVELOPMENT_V1",
    "DETERMINISTIC_REPLAY_FAULT_INJECTION",
    "NOT_EXPERT_VALIDATED",
]
DEVELOPMENT_LABELS = [
    "DEVELOPMENT_V1",
    "INCOMPLETE_DEVELOPMENT_ONLY",
    "NOT_EXPERT_VALIDATED",
]
LABELS = PUBLISH_LABELS  # Compatibility import; reports use mode-specific labels.
OFFICIAL_CASE_COUNT = 24
OFFICIAL_FAULT_COUNT = 12
REQUESTED_MODEL = "qwen3.7-plus-2026-05-26"
FALLBACK_MODEL = "qwen3.7-plus"
FALLBACK_REASON = "snapshot_unavailable"
OFFICIAL_DATA_SHA256 = {
    "cases": "bf8f07e3379d171ce0ac98b380dacef9c5597677eb74a0b444772632ebb22634",
    "gold": "79fce0ff7619e3f679d796531ca9675fe1909ff56484a6cd0f132dd000c371d7",
    "overlays": "fafe4a9a3902b2b8999cd339680deb16f2f2a9e4adcca4b4a3383858e79dd7f7",
}
OFFICIAL_RECORD_SHA256 = {
    "cases": "9ea51a8dc3f05e232acac92c0be400f7302750795d6f6143797fe0438b9ba70a",
    "gold": "abf0b733e9acb813f73f3b08cb1e672380706a6a66423f8614b0b15e498a229d",
    "overlays": "67795aa6ce37d793f11718152eb3bab223d2b6a095094e660fd1583b1bb0a69f",
}
PROMPT_ROLES = frozenset({"planner", "evidence", "diagnosis", "reviewer"})
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
ISSUE_TYPES = frozenset(
    {
        "unsupported_claim",
        "contradicted_claim",
        "failed_step",
        "plan_gap",
        "invalid_step",
    }
)

TOOL_ARGUMENTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "query_measurement": (frozenset({"key"}), frozenset()),
    "match_frequency_signature": (
        frozenset({"fundamental_mhz"}),
        frozenset({"peak_key", "tolerance_mhz"}),
    ),
    "compare_intervention": (frozenset({"intervention_id"}), frozenset()),
    "inspect_coupling_path": (frozenset({"path_id"}), frozenset()),
    "check_measurement_consistency": (
        frozenset({"measurement_key"}),
        frozenset({"tolerance_percent"}),
    ),
}
ALLOWED_TOOLS = frozenset(TOOL_ARGUMENTS)


class EvaluationError(ValueError):
    """The input cannot be scored or published without ambiguity."""


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not (line := raw_line.strip()):
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvaluationError(
                    f"{source.name}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise EvaluationError(f"{source.name}:{line_number}: expected object")
            records.append(value)
    return records


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value) if math.isfinite(float(value)) else default


def _integer(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def _rate(numerator: int | float, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(float(numerator) / denominator, 6) if denominator else None,
    }


def _hash_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_record_hash(record: Mapping[str, Any]) -> str:
    payload = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hash_records(records: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(records),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvaluationError(f"{location} must be an object")
    return value


def _reject_extra(
    value: Mapping[str, Any], allowed: set[str] | frozenset[str], location: str
) -> None:
    if extra := sorted(set(value) - set(allowed)):
        raise EvaluationError(f"{location} has unsupported fields: {extra}")


def _array(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvaluationError(f"{location} must be an array")
    return value


def _required_text(value: Any, location: str) -> str:
    if not (text := _text(value)):
        raise EvaluationError(f"{location} must be a non-empty string")
    return text


def _string_array(value: Any, location: str) -> list[str]:
    values = _array(value, location)
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise EvaluationError(f"{location} must contain non-empty strings")
    result = [item.strip() for item in values]
    if len(result) != len(set(result)):
        raise EvaluationError(f"{location} must not contain duplicates")
    return result


def _nonnegative_integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaluationError(f"{location} must be a non-negative integer")
    return value


def _nonnegative_number(value: Any, location: str) -> float:
    number = _number(value, -1)
    if number < 0:
        raise EvaluationError(f"{location} must be a finite non-negative number")
    return number


def _index_unique(
    records: Sequence[Mapping[str, Any]], key: str, label: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records, 1):
        identity = _required_text(record.get(key), f"{label}[{index}].{key}")
        if identity in result:
            raise EvaluationError(f"{label} has duplicate {key}: {identity}")
        result[identity] = record
    return result


def validate_case_gold_contract(
    case: Mapping[str, Any], gold: Mapping[str, Any], location: str = "case"
) -> dict[str, str]:
    """Validate evaluator-only joins and return the only legal tag-to-tool map."""

    case_id = _required_text(case.get("case_id"), f"{location}.case_id")
    if _required_text(gold.get("case_id"), f"{location}.gold.case_id") != case_id:
        raise EvaluationError(f"{location}: case/gold case_id mismatch")
    required_checks = _string_array(
        case.get("required_checks"), f"{location}.required_checks"
    )
    if not required_checks:
        raise EvaluationError(f"{location}.required_checks must not be empty")
    if unknown := set(required_checks) - ALLOWED_TOOLS:
        raise EvaluationError(f"{location}.required_checks has unknown tools: {sorted(unknown)}")

    source_ids: set[str] = set()
    observation_ids: set[str] = set()
    intervention_ids: set[str] = set()
    for collection in ("observations", "interventions"):
        for index, raw in enumerate(_array(case.get(collection), f"{location}.{collection}")):
            source = _object(raw, f"{location}.{collection}[{index}]")
            source_id = _required_text(
                source.get("id"), f"{location}.{collection}[{index}].id"
            )
            if source_id in source_ids:
                raise EvaluationError(f"{location} has duplicate runtime source id: {source_id}")
            source_ids.add(source_id)
            if collection == "observations":
                observation_ids.add(source_id)
            else:
                intervention_ids.add(source_id)

    tag_tools: dict[str, str] = {}
    tag_source_ids: dict[str, str] = {}
    tool_counts: Counter[str] = Counter()
    tool_data = _array(case.get("tool_data"), f"{location}.tool_data")
    if not tool_data:
        raise EvaluationError(f"{location}.tool_data must not be empty")
    for index, raw in enumerate(tool_data):
        item = _object(raw, f"{location}.tool_data[{index}]")
        item_at = f"{location}.tool_data[{index}]"
        source_id = _required_text(item.get("source_id"), f"{item_at}.source_id")
        if source_id not in source_ids:
            raise EvaluationError(f"{item_at}.source_id does not reference runtime data")
        tool = _required_text(item.get("tool"), f"{item_at}.tool")
        if tool not in ALLOWED_TOOLS:
            raise EvaluationError(f"{item_at}.tool must be a canonical tool")
        tool_counts[tool] += 1
        arguments = _object(item.get("arguments"), f"{item_at}.arguments")
        if reasons := _argument_reasons(tool, arguments):
            raise EvaluationError(f"{item_at}.arguments is not executable: {reasons}")
        payload = _object(item.get("payload"), f"{item_at}.payload")
        if not payload:
            raise EvaluationError(f"{item_at}.payload must not be empty")
        if tool == "query_measurement":
            measurement = _object(payload.get("measurement"), f"{item_at}.payload.measurement")
            if arguments.get("key") != source_id or measurement.get("id") != source_id:
                raise EvaluationError(f"{item_at} does not bind query key to source_id")
        elif tool == "match_frequency_signature":
            if arguments.get("peak_key") != source_id or payload.get("measurement_ref") != source_id:
                raise EvaluationError(f"{item_at} does not bind frequency input to source_id")
        elif tool == "compare_intervention":
            if arguments.get("intervention_id") != source_id or payload.get("intervention_id") != source_id:
                raise EvaluationError(f"{item_at} does not bind intervention to source_id")
        elif tool == "inspect_coupling_path":
            if (
                arguments.get("path_id") != payload.get("path_id")
                or payload.get("source_ref") != source_id
            ):
                raise EvaluationError(f"{item_at} does not bind coupling path to source_id")
        elif tool == "check_measurement_consistency":
            if (
                arguments.get("measurement_key") != source_id
                or payload.get("measurement_key") != source_id
            ):
                raise EvaluationError(f"{item_at} does not bind measurement to source_id")
        tags = _string_array(item.get("evidence_tags"), f"{item_at}.evidence_tags")
        if not tags:
            raise EvaluationError(f"{item_at}.evidence_tags must not be empty")
        for tag in tags:
            if tag in tag_tools:
                raise EvaluationError(
                    f"{location} evidence tag has more than one runtime source: {tag}"
                )
            tag_tools[tag] = tool
            tag_source_ids[tag] = source_id

    if any(tool_counts[tool] != 1 for tool in tool_counts) or any(
        tool_counts[tool] != 1 for tool in required_checks
    ):
        raise EvaluationError(
            f"{location}.tool_data must contain exactly one executable source per listed tool"
        )

    root_cause = _required_text(
        gold.get("root_cause_id"), f"{location}.gold.root_cause_id"
    )
    acceptable = _string_array(
        gold.get("acceptable_root_cause_ids"),
        f"{location}.gold.acceptable_root_cause_ids",
    )
    if not acceptable or root_cause not in acceptable:
        raise EvaluationError(
            f"{location}.gold.acceptable_root_cause_ids must contain root_cause_id"
        )
    required_tags = _string_array(
        gold.get("required_evidence_tags"),
        f"{location}.gold.required_evidence_tags",
    )
    if not required_tags:
        raise EvaluationError(f"{location}.gold.required_evidence_tags must not be empty")
    if missing := set(required_tags) - set(tag_tools):
        raise EvaluationError(
            f"{location}.gold tags lack a runtime evidence source: {sorted(missing)}"
        )
    if len(required_tags) != 2:
        raise EvaluationError(
            f"{location}.gold must define one observation/path tag and one intervention tag"
        )
    if tag_source_ids[required_tags[0]] not in observation_ids:
        raise EvaluationError(f"{location}.gold first tag must come from observation/path data")
    if tag_source_ids[required_tags[1]] not in intervention_ids:
        raise EvaluationError(f"{location}.gold second tag must come from intervention data")
    return tag_tools


def validate_overlay_contract(
    overlay: Mapping[str, Any], case: Mapping[str, Any], location: str = "overlay"
) -> None:
    _required_text(overlay.get("overlay_id"), f"{location}.overlay_id")
    if _required_text(overlay.get("case_id"), f"{location}.case_id") != case.get("case_id"):
        raise EvaluationError(f"{location}.case_id does not match its runtime case")
    fault_type = _required_text(overlay.get("fault_type"), f"{location}.fault_type")
    if fault_type not in {"transient_tool_error", "timeout_once", "process_interrupt"}:
        raise EvaluationError(f"{location}.fault_type is unsupported")
    target = _object(overlay.get("target"), f"{location}.target")
    trigger = _object(overlay.get("trigger"), f"{location}.trigger")
    if trigger.get("attempt") != 1:
        raise EvaluationError(f"{location}.trigger.attempt must equal 1")
    _string_array(overlay.get("success_conditions"), f"{location}.success_conditions")
    pair_contract = _object(overlay.get("pair_contract"), f"{location}.pair_contract")
    for variant in VARIANTS:
        _required_text(pair_contract.get(variant), f"{location}.pair_contract.{variant}")

    if fault_type in {"transient_tool_error", "timeout_once"}:
        if target.get("node") != "evidence_worker":
            raise EvaluationError(f"{location}.target.node must be evidence_worker")
        tool = _required_text(target.get("tool"), f"{location}.target.tool")
        if tool not in ALLOWED_TOOLS or tool not in set(_list(case.get("required_checks"))):
            raise EvaluationError(f"{location}.target.tool must be required by its case")
        if target.get("selector") != "first_matching_tool":
            raise EvaluationError(
                f"{location}.target.selector must be first_matching_tool"
            )
        if "operation_id" in target or "step_id" in target:
            raise EvaluationError(
                f"{location}.target must not predict dynamic operation_id or step_id"
            )
        expected_mode = "raise_once" if fault_type == "transient_tool_error" else "timeout_once"
        if trigger.get("mode") != expected_mode:
            raise EvaluationError(f"{location}.trigger.mode must be {expected_mode}")
        if fault_type == "timeout_once" and _nonnegative_integer(
            trigger.get("timeout_ms"), f"{location}.trigger.timeout_ms"
        ) < 1:
            raise EvaluationError(f"{location}.trigger.timeout_ms must be positive")
    else:
        _required_text(target.get("node"), f"{location}.target.node")
        _required_text(target.get("after_node"), f"{location}.target.after_node")
        if trigger.get("mode") != "terminate_after_checkpoint":
            raise EvaluationError(
                f"{location}.trigger.mode must be terminate_after_checkpoint"
            )


def _argument_reasons(tool: str, arguments: Mapping[str, Any]) -> list[str]:
    if tool not in TOOL_ARGUMENTS:
        return ["tool_not_allowed"]
    required, optional = TOOL_ARGUMENTS[tool]
    keys = set(arguments)
    reasons: list[str] = []
    if missing := sorted(required - keys):
        reasons.append("missing_arguments:" + ",".join(missing))
    if extra := sorted(keys - required - optional):
        reasons.append("unexpected_arguments:" + ",".join(extra))

    def nonempty(name: str) -> None:
        if name in arguments and not _text(arguments.get(name)):
            reasons.append(f"invalid_argument_type:{name}")

    if tool == "query_measurement":
        nonempty("key")
    elif tool == "match_frequency_signature":
        fundamental = _number(arguments.get("fundamental_mhz"), -1)
        if fundamental <= 0:
            reasons.append("invalid_argument_range:fundamental_mhz")
        if "peak_key" in arguments:
            nonempty("peak_key")
        if "tolerance_mhz" in arguments:
            tolerance = _number(arguments["tolerance_mhz"], -1)
            if not 0 < tolerance <= max(5.0, fundamental * 0.1):
                reasons.append("invalid_argument_range:tolerance_mhz")
    elif tool == "compare_intervention":
        nonempty("intervention_id")
    elif tool == "inspect_coupling_path":
        nonempty("path_id")
    elif tool == "check_measurement_consistency":
        nonempty("measurement_key")
        if "tolerance_percent" in arguments and not 0 < _number(arguments["tolerance_percent"], -1) <= 100:
            reasons.append("invalid_argument_range:tolerance_percent")
    return sorted(set(reasons))


def _validate_provenance(run: Mapping[str, Any], location: str) -> None:
    provenance = _object(run.get("provenance"), f"{location}.provenance")
    for key in ("execution_mode", "provider", "model"):
        _required_text(provenance.get(key), f"{location}.provenance.{key}")
    if provenance["execution_mode"] not in {"live", "fixture", "replay"}:
        raise EvaluationError(f"{location}.provenance.execution_mode is unsupported")
    for key in ("config_hash", "data_hash"):
        if not _is_sha256(provenance.get(key)):
            raise EvaluationError(f"{location}.provenance.{key} must be SHA-256")
    hashes = _object(provenance.get("prompt_hashes"), f"{location}.provenance.prompt_hashes")
    if not hashes or any(not _text(role) or not _is_sha256(digest) for role, digest in hashes.items()):
        raise EvaluationError(f"{location}.provenance.prompt_hashes is invalid")


def _validate_evidence(run: Mapping[str, Any], location: str) -> None:
    steps = {
        _text(_dict(item).get("step_id")): _dict(item)
        for item in _list(_dict(run.get("plan")).get("steps"))
        if _text(_dict(item).get("step_id"))
    }
    operations: set[str] = set()
    for index, raw in enumerate(_array(run.get("evidence"), f"{location}.evidence")):
        item = _object(raw, f"{location}.evidence[{index}]")
        item_at = f"{location}.evidence[{index}]"
        operation = _required_text(item.get("operation_id"), f"{item_at}.operation_id")
        if operation in operations:
            raise EvaluationError(f"{location} has duplicate operation_id: {operation}")
        operations.add(operation)
        if "evidence_id" in item and item["evidence_id"] != operation:
            raise EvaluationError(f"{item_at}.evidence_id must equal operation_id")
        step = _required_text(item.get("step_id"), f"{item_at}.step_id")
        if step not in steps:
            raise EvaluationError(f"{item_at}.step_id does not reference the plan")
        tool = _required_text(item.get("tool"), f"{item_at}.tool")
        if tool not in ALLOWED_TOOLS:
            raise EvaluationError(f"{item_at}.tool is not allowed")
        if item.get("phase") not in {"initial", "rework"}:
            raise EvaluationError(f"{item_at}.phase must be initial or rework")
        if item["phase"] == "initial" and tool != _text(steps[step].get("tool")):
            raise EvaluationError(f"{item_at}.tool does not match its plan step")
        if item.get("status") not in {"success", "failure"}:
            raise EvaluationError(f"{item_at}.status must be success or failure")
        tags = _string_array(item.get("evidence_tags"), f"{item_at}.evidence_tags")
        if item["status"] == "success" and not tags:
            raise EvaluationError(f"{item_at}.evidence_tags must not be empty on success")
        if item["status"] == "failure" and tags:
            raise EvaluationError(f"{item_at}.evidence_tags must be empty on failure")
        attempt = item.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise EvaluationError(f"{item_at}.attempt must be positive integer")


def _validate_diagnosis(
    value: Any, operation_ids: set[str], location: str, *, allow_empty: bool
) -> None:
    diagnosis = _object(value, location)
    _reject_extra(diagnosis, {"candidates", "claims"}, location)
    if "root_causes" in diagnosis:
        raise EvaluationError(f"{location} must use candidates, not Graph-state root_causes")
    candidates = _array(diagnosis.get("candidates"), f"{location}.candidates")
    claims = _array(diagnosis.get("claims"), f"{location}.claims")
    if not allow_empty and (not candidates or not claims):
        raise EvaluationError(f"{location} must contain candidates and claims")
    causes: set[str] = set()
    ranks: set[int] = set()
    for index, raw in enumerate(candidates):
        item = _object(raw, f"{location}.candidates[{index}]")
        item_at = f"{location}.candidates[{index}]"
        _reject_extra(
            item,
            {"root_cause_id", "rank", "confidence", "evidence_ids", "label", "rationale"},
            item_at,
        )
        cause = _required_text(item.get("root_cause_id"), f"{item_at}.root_cause_id")
        if cause in causes:
            raise EvaluationError(f"{location} has duplicate cause_id: {cause}")
        causes.add(cause)
        rank = item.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1 or rank in ranks:
            raise EvaluationError(f"{item_at}.rank must be a unique positive integer")
        ranks.add(rank)
        confidence = _number(item.get("confidence"), -1)
        if not 0 <= confidence <= 1:
            raise EvaluationError(f"{item_at}.confidence must be in [0,1]")
        evidence_ids = _string_array(item.get("evidence_ids"), f"{item_at}.evidence_ids")
        if unknown := set(evidence_ids) - operation_ids:
            raise EvaluationError(f"{item_at} references unknown operation_ids: {sorted(unknown)}")
        for optional_text in ("label", "rationale"):
            if optional_text in item:
                _required_text(item.get(optional_text), f"{item_at}.{optional_text}")
    claim_ids: set[str] = set()
    for index, raw in enumerate(claims):
        item = _object(raw, f"{location}.claims[{index}]")
        item_at = f"{location}.claims[{index}]"
        _reject_extra(
            item,
            {"claim_id", "text", "evidence_ids", "contradicting_evidence_ids"},
            item_at,
        )
        claim = _required_text(item.get("claim_id"), f"{item_at}.claim_id")
        if claim in claim_ids:
            raise EvaluationError(f"{location} has duplicate claim_id: {claim}")
        claim_ids.add(claim)
        _required_text(item.get("text"), f"{item_at}.text")
        if "contradicted_by" in item:
            raise EvaluationError(
                f"{item_at} must use contradicting_evidence_ids, not Graph-state contradicted_by"
            )
        supports = _string_array(item.get("evidence_ids"), f"{item_at}.evidence_ids")
        contradicts = _string_array(
            item.get("contradicting_evidence_ids"),
            f"{item_at}.contradicting_evidence_ids",
        )
        if unknown := (set(supports) | set(contradicts)) - operation_ids:
            raise EvaluationError(f"{item_at} references unknown operation_ids: {sorted(unknown)}")


def _validate_review(run: Mapping[str, Any], location: str) -> None:
    review = _object(run.get("review"), f"{location}.review")
    _reject_extra(review, {"initial_diagnosis", "issues"}, f"{location}.review")
    operation_ids = {
        _text(item.get("operation_id"))
        for item in _list(run.get("evidence"))
        if isinstance(item, dict) and item.get("phase") == "initial"
    }
    _validate_diagnosis(
        review.get("initial_diagnosis"), operation_ids,
        f"{location}.review.initial_diagnosis", allow_empty=True,
    )
    initial_claims = {
        _text(_dict(item).get("claim_id"))
        for item in _list(_dict(review.get("initial_diagnosis")).get("claims"))
    }
    steps = {
        _text(_dict(item).get("step_id"))
        for item in _list(_dict(run.get("plan")).get("steps"))
    }
    issue_ids: set[str] = set()
    targets: set[tuple[str, str]] = set()
    for index, raw in enumerate(_array(review.get("issues"), f"{location}.review.issues")):
        item = _object(raw, f"{location}.review.issues[{index}]")
        item_at = f"{location}.review.issues[{index}]"
        _reject_extra(
            item,
            {"issue_id", "issue_type", "target_id", "resolved", "description"},
            item_at,
        )
        issue_id = _required_text(item.get("issue_id"), f"{item_at}.issue_id")
        if issue_id in issue_ids:
            raise EvaluationError(f"{location} has duplicate issue_id: {issue_id}")
        issue_ids.add(issue_id)
        issue_type = item.get("issue_type")
        if issue_type not in ISSUE_TYPES:
            raise EvaluationError(f"{item_at}.issue_type is unsupported")
        target = _required_text(item.get("target_id"), f"{item_at}.target_id")
        if issue_type in {"unsupported_claim", "contradicted_claim"} and target not in initial_claims:
            raise EvaluationError(f"{item_at}.target_id must reference an initial claim")
        if issue_type in {"failed_step", "invalid_step"} and target not in steps:
            raise EvaluationError(f"{item_at}.target_id must reference a plan step")
        if issue_type == "plan_gap" and target not in ALLOWED_TOOLS:
            raise EvaluationError(f"{item_at}.target_id must be an allowed tool")
        key = (str(issue_type), target)
        if key in targets:
            raise EvaluationError(f"{location} has duplicate reviewer issue target: {key}")
        targets.add(key)
        if not isinstance(item.get("resolved"), bool):
            raise EvaluationError(f"{item_at}.resolved must be boolean")
        if "description" in item:
            _required_text(item.get("description"), f"{item_at}.description")


def _validate_metrics(run: Mapping[str, Any], location: str) -> None:
    metrics = _object(run.get("metrics"), f"{location}.metrics")
    for key in ("model_calls", "input_tokens", "output_tokens"):
        _nonnegative_integer(metrics.get(key), f"{location}.metrics.{key}")
    _nonnegative_number(metrics.get("latency_ms"), f"{location}.metrics.latency_ms")


def _validate_trajectory(run: Mapping[str, Any], location: str) -> None:
    keys: set[str] = set()
    for index, raw in enumerate(_array(run.get("trajectory"), f"{location}.trajectory")):
        item = _object(raw, f"{location}.trajectory[{index}]")
        item_at = f"{location}.trajectory[{index}]"
        _reject_extra(item, {"event_key", "node", "detail", "timestamp"}, item_at)
        event_key = _required_text(item.get("event_key"), f"{item_at}.event_key")
        if event_key in keys:
            raise EvaluationError(f"{location} has duplicate event_key: {event_key}")
        keys.add(event_key)
        _required_text(item.get("node"), f"{item_at}.node")
        _required_text(item.get("timestamp"), f"{item_at}.timestamp")
        detail = _object(item.get("detail"), f"{item_at}.detail")
        _required_text(detail.get("event_type"), f"{item_at}.detail.event_type")


def validate_run_contract(run: Mapping[str, Any], location: str = "run") -> None:
    _required_text(run.get("run_id"), f"{location}.run_id")
    _required_text(run.get("case_id"), f"{location}.case_id")
    if run.get("variant") not in VARIANTS:
        raise EvaluationError(f"{location}.variant is invalid")
    if run.get("run_kind") not in RUN_KINDS:
        raise EvaluationError(f"{location}.run_kind is invalid")
    if run.get("status") not in {"completed", "needs_human_review", "failed", "interrupted"}:
        raise EvaluationError(f"{location}.status is invalid")
    plan = _object(run.get("plan"), f"{location}.plan")
    _array(plan.get("hypotheses"), f"{location}.plan.hypotheses")
    _array(plan.get("steps"), f"{location}.plan.steps")
    _validate_evidence(run, location)
    operations = {
        _text(item.get("operation_id")) for item in _list(run.get("evidence")) if isinstance(item, dict)
    }
    _validate_diagnosis(
        run.get("diagnosis"), operations, f"{location}.diagnosis",
        allow_empty=run.get("status") != "completed",
    )
    _validate_review(run, location)
    _validate_metrics(run, location)
    _validate_provenance(run, location)
    _validate_trajectory(run, location)


def _index_runs(
    runs: Sequence[Mapping[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
    overlays: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], Mapping[str, Any]], dict[tuple[str, str], Mapping[str, Any]]]:
    normal: dict[tuple[str, str], Mapping[str, Any]] = {}
    faults: dict[tuple[str, str], Mapping[str, Any]] = {}
    run_ids: set[str] = set()
    for index, run in enumerate(runs, 1):
        location = f"runs[{index}]"
        validate_run_contract(run, location)
        run_id = _text(run.get("run_id"))
        if run_id in run_ids:
            raise EvaluationError(f"duplicate run_id: {run_id}")
        run_ids.add(run_id)
        case_id = _text(run.get("case_id"))
        variant = _text(run.get("variant"))
        if case_id not in cases:
            raise EvaluationError(f"{location} refers to unknown case: {case_id}")
        if run["run_kind"] == "evaluation":
            key = (variant, case_id)
            if key in normal:
                raise EvaluationError(f"duplicate evaluation run: {key}")
            normal[key] = run
        else:
            fault = _object(run.get("fault"), f"{location}.fault")
            overlay_id = _required_text(fault.get("overlay_id"), f"{location}.fault.overlay_id")
            if overlay_id not in overlays:
                raise EvaluationError(f"{location} refers to unknown overlay: {overlay_id}")
            if overlays[overlay_id].get("case_id") != case_id:
                raise EvaluationError(f"{location}.case_id does not match its overlay")
            key = (variant, overlay_id)
            if key in faults:
                raise EvaluationError(f"duplicate fault run: {key}")
            faults[key] = run
    return normal, faults


def score_plan(run: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any]:
    plan = _dict(run.get("plan"))
    hypotheses = _list(plan.get("hypotheses"))
    steps = _list(plan.get("steps"))
    hypothesis_ids: set[str] = set()
    duplicate_hypotheses: set[str] = set()
    for raw in hypotheses:
        identity = _text(_dict(raw).get("hypothesis_id"))
        if identity in hypothesis_ids:
            duplicate_hypotheses.add(identity)
        if identity:
            hypothesis_ids.add(identity)

    seen: set[str] = set()
    signatures: set[str] = set()
    covered_hypotheses: set[str] = set()
    tools: set[str] = set()
    invalid: list[dict[str, Any]] = []
    for index, raw in enumerate(steps):
        step = _dict(raw)
        step_id = _text(step.get("step_id"))
        hypothesis_id = _text(step.get("hypothesis_id"))
        tool = _text(step.get("tool"))
        arguments = _dict(step.get("arguments"))
        dependencies = _list(step.get("depends_on"))
        reasons: list[str] = []
        if not step_id:
            reasons.append("missing_step_id")
        elif step_id in seen:
            reasons.append("duplicate_step_id")
        if not hypothesis_id or hypothesis_id not in hypothesis_ids:
            reasons.append("unknown_hypothesis_id")
        else:
            covered_hypotheses.add(hypothesis_id)
        reasons.extend(_argument_reasons(tool, arguments))
        if tool in ALLOWED_TOOLS:
            tools.add(tool)
        if any(not isinstance(dep, str) or dep not in seen for dep in dependencies):
            reasons.append("dependency_missing_or_not_prior")
        if step_id and step_id in dependencies:
            reasons.append("self_dependency")
        if not _text(step.get("completion_condition")):
            reasons.append("missing_completion_condition")
        signature = json.dumps(
            {"tool": tool, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if signature in signatures:
            reasons.append("duplicate_or_redundant_step")
        signatures.add(signature)
        if reasons:
            invalid.append({"step_id": step_id or f"index-{index}", "reasons": sorted(set(reasons))})
        if step_id:
            seen.add(step_id)

    uncovered = sorted(hypothesis_ids - covered_hypotheses)
    plan_reasons: list[str] = []
    if not hypotheses:
        plan_reasons.append("empty_hypotheses")
    if duplicate_hypotheses:
        plan_reasons.append("duplicate_hypothesis_id")
    if not steps:
        plan_reasons.append("empty_plan")
    if uncovered:
        plan_reasons.append("uncovered_hypothesis")
    return {
        "executable": bool(hypotheses)
        and bool(steps)
        and not invalid
        and not uncovered
        and not duplicate_hypotheses,
        "invalid_steps": invalid,
        "invalid_step_count": len(invalid),
        "total_step_count": len(steps),
        "missing_required_checks": [],
        "uncovered_hypotheses": uncovered,
        "plan_reasons": plan_reasons,
    }


def _successful_evidence(
    run: Mapping[str, Any], *, initial_only: bool = False
) -> list[Mapping[str, Any]]:
    return [
        item
        for item in _list(run.get("evidence"))
        if isinstance(item, dict)
        and item.get("status") == "success"
        and (not initial_only or item.get("phase") == "initial")
    ]


def classify_claims(
    claims: Sequence[Any], evidence: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    successful = {_text(item.get("operation_id")) for item in evidence}
    statuses: dict[str, str] = {}
    for raw in claims:
        claim = _dict(raw)
        claim_id = _text(claim.get("claim_id"))
        supports = set(_list(claim.get("evidence_ids"))) & successful
        contradictions = set(_list(claim.get("contradicting_evidence_ids"))) & successful
        statuses[claim_id] = (
            "contradicted" if contradictions else "supported" if supports else "unsupported"
        )
    return statuses


def _score_review(
    run: Mapping[str, Any], plan_score: Mapping[str, Any], final_statuses: Mapping[str, str]
) -> dict[str, Any]:
    review = _dict(run.get("review"))
    initial_evidence = _successful_evidence(run, initial_only=True)
    final_evidence = _successful_evidence(run)
    initial_claims = _list(_dict(review.get("initial_diagnosis")).get("claims"))
    initial_statuses = classify_claims(initial_claims, initial_evidence)
    initial_steps = {_text(item.get("step_id")) for item in initial_evidence}
    final_steps = {_text(item.get("step_id")) for item in final_evidence}
    final_tools = {_text(item.get("tool")) for item in final_evidence}

    derived: dict[tuple[str, str], dict[str, Any]] = {}
    for claim_id, status in initial_statuses.items():
        if status in {"unsupported", "contradicted"}:
            issue_type = "unsupported_claim" if status == "unsupported" else "contradicted_claim"
            derived[(issue_type, claim_id)] = {
                "issue_type": issue_type,
                "target_id": claim_id,
                "resolved_by_state": claim_id not in final_statuses
                or final_statuses.get(claim_id) == "supported",
            }
    for item in plan_score.get("invalid_steps", []):
        step_id = _text(_dict(item).get("step_id"))
        if step_id and not step_id.startswith("index-"):
            derived[("invalid_step", step_id)] = {
                "issue_type": "invalid_step",
                "target_id": step_id,
                "resolved_by_state": False,
            }
    for raw in _list(_dict(run.get("plan")).get("steps")):
        step_id = _text(_dict(raw).get("step_id"))
        if step_id and step_id not in initial_steps:
            derived[("failed_step", step_id)] = {
                "issue_type": "failed_step",
                "target_id": step_id,
                "resolved_by_state": step_id in final_steps,
            }
    for tool in plan_score.get("missing_required_checks", []):
        derived[("plan_gap", str(tool))] = {
            "issue_type": "plan_gap",
            "target_id": str(tool),
            "resolved_by_state": tool in final_tools,
        }

    reported: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw in _list(review.get("issues")):
        issue = _dict(raw)
        key = (_text(issue.get("issue_type")), _text(issue.get("target_id")))
        reported[key] = issue
    scored: list[dict[str, Any]] = []
    for key, issue in sorted(derived.items()):
        declaration = reported.get(key)
        scored.append(
            {
                **issue,
                "issue_id": _text(_dict(declaration).get("issue_id")) if declaration else "",
                "reported": declaration is not None,
                "declared_resolved": _dict(declaration).get("resolved") if declaration else None,
                "valid": True,
                "resolved": bool(declaration is not None and issue["resolved_by_state"]),
            }
        )
    for key, issue in sorted(reported.items()):
        if key not in derived:
            scored.append(
                {
                    "issue_id": _text(issue.get("issue_id")),
                    "issue_type": key[0],
                    "target_id": key[1],
                    "reported": True,
                    "declared_resolved": issue.get("resolved"),
                    "valid": False,
                    "resolved": False,
                    "resolved_by_state": False,
                }
            )
    return {
        "issues": scored,
        "valid_count": len(derived),
        "reported_valid_count": sum(item["valid"] and item["reported"] for item in scored),
        "resolved_count": sum(item["valid"] and item["resolved"] for item in scored),
        "unresolved_valid_count": sum(item["valid"] and not item["resolved"] for item in scored),
        "initial_claim_statuses": initial_statuses,
    }


def _top_candidate(run: Mapping[str, Any]) -> Mapping[str, Any]:
    candidates = [_dict(item) for item in _list(_dict(run.get("diagnosis")).get("candidates"))]
    if not candidates:
        return {}
    candidates.sort(
        key=lambda item: (
            _integer(item.get("rank"), 10**9),
            -_number(item.get("confidence"), -1),
            _text(item.get("root_cause_id")),
        )
    )
    return candidates[0]


def score_run(
    run: Mapping[str, Any], case: Mapping[str, Any], gold: Mapping[str, Any]
) -> dict[str, Any]:
    validate_run_contract(run)
    tag_tools = validate_case_gold_contract(case, gold)
    plan_score = score_plan(run, case)
    successful = _successful_evidence(run)
    for evidence in successful:
        tool = _text(evidence.get("tool"))
        for tag in _list(evidence.get("evidence_tags")):
            if not isinstance(tag, str) or tag_tools.get(tag) != tool:
                raise EvaluationError(
                    "successful evidence tag is not emitted by its canonical runtime tool: "
                    f"{tool}:{tag}"
                )
    claims = _list(_dict(run.get("diagnosis")).get("claims"))
    claim_statuses = classify_claims(claims, successful)
    review_score = _score_review(run, plan_score, claim_statuses)
    evidence_tags = {
        tag
        for item in successful
        for tag in _list(item.get("evidence_tags"))
        if isinstance(tag, str)
    }
    required_tags = set(_list(gold.get("required_evidence_tags")))
    top_candidate = _top_candidate(run)
    top_candidate_id = _text(top_candidate.get("root_cause_id"))
    successful_operations = {_text(item.get("operation_id")) for item in successful}
    top_candidate_grounded = bool(
        set(_list(top_candidate.get("evidence_ids"))) & successful_operations
    )
    top1_hit = bool(
        top_candidate_id
        and top_candidate_id in set(_list(gold.get("acceptable_root_cause_ids")))
    )
    all_claims_supported = bool(claim_statuses) and all(
        status == "supported" for status in claim_statuses.values()
    )
    task_completed = bool(
        run.get("status") == "completed"
        and plan_score["executable"]
        and top_candidate_id
        and top_candidate_grounded
        and required_tags.issubset(evidence_tags)
        and all_claims_supported
        and review_score["unresolved_valid_count"] == 0
    )
    bad_claims = sum(status in {"unsupported", "contradicted"} for status in claim_statuses.values())
    metrics = _dict(run.get("metrics"))
    failures: list[str] = []
    if not plan_score["executable"]:
        failures.append("plan_not_executable")
    if not required_tags.issubset(evidence_tags):
        failures.append("required_evidence_tags_missing")
    if not all_claims_supported:
        failures.append("claims_not_fully_supported")
    if not top_candidate_grounded:
        failures.append("top_candidate_not_grounded")
    if not task_completed:
        failures.append("task_incomplete")
    if not top1_hit:
        failures.append("top1_miss")
    if bad_claims:
        failures.append("unsupported_or_contradicted_claim")
    if review_score["unresolved_valid_count"]:
        failures.append("review_issue_unresolved")
    return {
        "present": True,
        "status": _text(run.get("status")),
        "plan": plan_score,
        "task_completed": task_completed,
        "top1_root_cause_id": top_candidate_id,
        "top_candidate_grounded": top_candidate_grounded,
        "top1_hit": top1_hit,
        "claim_statuses": claim_statuses,
        "bad_claim_count": bad_claims,
        "auditable_claim_count": len(claim_statuses),
        "review": review_score,
        "metrics": {
            "model_calls": _integer(metrics.get("model_calls")),
            "input_tokens": _integer(metrics.get("input_tokens")),
            "output_tokens": _integer(metrics.get("output_tokens")),
            "latency_ms": _number(metrics.get("latency_ms")),
        },
        "missing_required_evidence_tags": sorted(required_tags - evidence_tags),
        "failures": sorted(set(failures)),
    }


def _missing_run_score() -> dict[str, Any]:
    return {
        "present": False,
        "status": "missing",
        "plan": {
            "executable": False,
            "invalid_steps": [],
            "invalid_step_count": 0,
            "total_step_count": 0,
            "missing_required_checks": [],
            "uncovered_hypotheses": [],
            "plan_reasons": ["missing_run"],
        },
        "task_completed": False,
        "top1_root_cause_id": "",
        "top_candidate_grounded": False,
        "top1_hit": False,
        "claim_statuses": {},
        "bad_claim_count": 0,
        "auditable_claim_count": 0,
        "review": {
            "issues": [], "valid_count": 0, "reported_valid_count": 0,
            "resolved_count": 0, "unresolved_valid_count": 0, "initial_claim_statuses": {},
        },
        "metrics": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0.0},
        "missing_required_evidence_tags": [],
        "failures": ["missing_run", "plan_not_executable", "task_incomplete", "top1_miss"],
    }


def _events(run: Mapping[str, Any], event_type: str) -> list[Mapping[str, Any]]:
    return [
        item
        for item in _list(run.get("trajectory"))
        if isinstance(item, dict)
        and _dict(item.get("detail")).get("event_type") == event_type
    ]


def _fault_proof(run: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    overlay_id = _text(overlay.get("overlay_id"))
    fault_type = _text(overlay.get("fault_type"))
    target = _dict(overlay.get("target"))
    target_tool = _text(target.get("tool"))
    target_node = _text(target.get("node"))
    trajectory = [item for item in _list(run.get("trajectory")) if isinstance(item, dict)]
    injected = [
        event
        for event in _events(run, "fault_injected")
        if _dict(event.get("detail")).get("overlay_id") == overlay_id
        and _dict(event.get("detail")).get("fault_type") == fault_type
        and event.get("node") == target_node
        and (
            fault_type == "process_interrupt"
            or _dict(event.get("detail")).get("tool") == target_tool
        )
    ]
    triggered = len(injected) == 1
    failures: list[str] = [] if triggered else ["fault_event_missing_or_duplicated"]
    manual = any(
        _dict(item.get("detail")).get("event_type")
        in {"manual_intervention", "human_intervention"}
        for item in trajectory
    )
    if manual:
        failures.append("manual_intervention_observed")
    proof_ok = False
    proof: dict[str, Any] = {"fault_event_count": len(injected)}
    if fault_type in {"transient_tool_error", "timeout_once"}:
        injected_detail = _dict(injected[0].get("detail")) if triggered else {}
        actual_operation = _text(injected_detail.get("operation_id"))
        actual_step = _text(injected_detail.get("step_id"))
        attempts = [
            event
            for event in _events(run, "tool_attempt")
            if _dict(event.get("detail")).get("operation_id") == actual_operation
            and _dict(event.get("detail")).get("tool") == target_tool
            and _dict(event.get("detail")).get("step_id") == actual_step
        ]
        outcomes = [_text(_dict(item.get("detail")).get("outcome")) for item in attempts]
        ordinals = [
            _integer(_dict(item.get("detail")).get("tool_attempt"), -1)
            for item in attempts
        ]
        first = "failure" if fault_type == "transient_tool_error" else "timeout"
        successful_operations = {
            _text(item.get("operation_id")) for item in _successful_evidence(run)
        }
        proof_ok = bool(
            triggered
            and actual_operation
            and actual_step
            and len(attempts) == 2
            and ordinals == [1, 2]
            and outcomes == [first, "success"]
            and actual_operation in successful_operations
        )
        proof.update(
            {
                "operation_id": actual_operation,
                "step_id": actual_step,
                "attempt_count": len(attempts),
                "attempt_ordinals": ordinals,
                "attempt_outcomes": outcomes,
            }
        )
        if not proof_ok:
            failures.append("retry_trajectory_not_proven")
    elif fault_type == "process_interrupt":
        resumes = _events(run, "checkpoint_resume")
        resume_event = resumes[0] if len(resumes) == 1 else {}
        resume_detail = _dict(_dict(resume_event).get("detail"))
        checkpoint_resume = _dict(resume_detail.get("checkpoint_resume"))
        resume_from = _dict(checkpoint_resume.get("from"))
        resume_to = _dict(checkpoint_resume.get("to"))
        prior = _text(resume_from.get("process_instance_id"))
        current = _text(resume_to.get("process_instance_id"))
        checkpoint = _text(resume_from.get("checkpoint_id"))
        resumed_from = _text(resume_to.get("resumed_from_checkpoint_id"))
        resume_index = trajectory.index(resume_event) if resume_event in trajectory else -1
        injected_index = trajectory.index(injected[0]) if triggered else -1
        injected_detail = _dict(injected[0].get("detail")) if triggered else {}
        injected_checkpoint = _text(injected_detail.get("checkpoint_id"))
        before: set[str] = set()
        after: set[str] = set()
        after_node = _text(target.get("after_node"))
        after_node_completed_before_fault = False
        for event_index, event in enumerate(trajectory):
            detail = _dict(event.get("detail"))
            if detail.get("event_type") != "node_completed" or detail.get("outcome") != "success":
                continue
            node = _text(detail.get("node"))
            instance = _text(detail.get("process_instance_id"))
            if node and event_index < resume_index and instance == prior:
                before.add(node)
            if (
                node == after_node
                and event_index < injected_index
                and instance == prior
            ):
                after_node_completed_before_fault = True
            if node and event_index > resume_index and instance == current:
                after.add(node)
        reexecuted = sorted(before & after)
        injected_instance = (
            _text(injected_detail.get("process_instance_id"))
            if triggered
            else ""
        )
        proof_ok = bool(
            triggered
            and len(resumes) == 1
            and 0 <= injected_index < resume_index
            and prior
            and current
            and prior != current
            and injected_instance == prior
            and checkpoint
            and injected_checkpoint == checkpoint
            and checkpoint == resumed_from
            and after_node_completed_before_fault
            and not reexecuted
        )
        proof.update(
            {
                "prior_process_instance_id": prior,
                "resumed_process_instance_id": current,
                "fault_checkpoint_id": injected_checkpoint,
                "from_checkpoint_id": checkpoint,
                "resumed_from_checkpoint_id": resumed_from,
                "after_node_completed_before_fault": after_node_completed_before_fault,
                "successful_node_keys_before_resume": sorted(before),
                "reexecuted_successful_node_keys": reexecuted,
            }
        )
        if not proof_ok:
            failures.append("cross_instance_checkpoint_resume_not_proven")
    else:
        failures.append("unknown_fault_type")
    return {
        "triggered": triggered,
        "proof_ok": proof_ok,
        "manual_intervention": manual,
        "proof": proof,
        "failures": failures,
    }


def score_fault_run(
    run: Mapping[str, Any] | None,
    overlay: Mapping[str, Any],
    case: Mapping[str, Any],
    gold: Mapping[str, Any],
) -> dict[str, Any]:
    validate_overlay_contract(overlay, case)
    if run is None:
        return {
            "present": False, "fault_triggered": False, "recovered": False,
            "proof": {}, "run_score": _missing_run_score(), "failures": ["missing_fault_run"],
        }
    if run.get("run_kind") != "fault_injection":
        raise EvaluationError("fault scorer requires run_kind=fault_injection")
    fault = _object(run.get("fault"), "run.fault")
    if fault.get("overlay_id") != overlay.get("overlay_id"):
        raise EvaluationError("fault run overlay_id does not match the scored overlay")
    run_score = score_run(run, case, gold)
    proof = _fault_proof(run, overlay)
    operations = [_text(item.get("operation_id")) for item in _successful_evidence(run)]
    duplicates = sorted(key for key, count in Counter(operations).items() if key and count > 1)
    recovered = bool(
        proof["triggered"] and proof["proof_ok"] and not proof["manual_intervention"]
        and not duplicates and run_score["task_completed"] and run_score["top1_hit"]
    )
    failures = list(proof["failures"])
    if duplicates:
        failures.append("duplicate_successful_operation")
    if not run_score["task_completed"]:
        failures.append("task_incomplete")
    if not run_score["top1_hit"]:
        failures.append("top1_miss")
    return {
        "present": True,
        "fault_triggered": proof["triggered"],
        "recovered": recovered,
        "proof": proof["proof"],
        "duplicate_successful_operation_ids": duplicates,
        "run_score": run_score,
        "failures": sorted(set(failures)),
    }


def _distribution(values: Sequence[float | int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "total": 0, "mean": None, "median": None, "min": None, "max": None, "range": None}
    normalized = [float(value) for value in values]
    return {
        "count": len(normalized),
        "total": round(sum(normalized), 3),
        "mean": round(statistics.fmean(normalized), 3),
        "median": round(statistics.median(normalized), 3),
        "min": round(min(normalized), 3),
        "max": round(max(normalized), 3),
        "range": round(max(normalized) - min(normalized), 3),
    }


def _aggregate_variant(
    normal: Sequence[Mapping[str, Any]], faults: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    invalid = sum(item["plan"]["invalid_step_count"] for item in normal)
    steps = sum(item["plan"]["total_step_count"] for item in normal)
    macro = [
        item["plan"]["invalid_step_count"] / item["plan"]["total_step_count"]
        for item in normal if item["plan"]["total_step_count"]
    ]
    present = [item for item in normal if item["present"]]
    bad_claims = sum(item["bad_claim_count"] for item in normal)
    claims = sum(item["auditable_claim_count"] for item in normal)
    valid_issues = sum(item["review"]["valid_count"] for item in normal)
    resolved_issues = sum(item["review"]["resolved_count"] for item in normal)
    return {
        "observed_normal_runs": sum(item["present"] for item in normal),
        "expected_normal_runs": len(normal),
        "plan_executable_rate": _rate(sum(item["plan"]["executable"] for item in normal), len(normal)),
        "invalid_step_rate_micro": _rate(invalid, steps),
        "invalid_step_rate_macro": {
            "numerator": round(sum(macro), 6),
            "denominator": len(macro),
            "value": round(statistics.fmean(macro), 6) if macro else None,
            "numerator_semantics": "sum_of_per_case_invalid_step_rates",
        },
        "task_completion_rate": _rate(sum(item["task_completed"] for item in normal), len(normal)),
        "top1_root_cause_hit_rate": _rate(sum(item["top1_hit"] for item in normal), len(normal)),
        "unsupported_or_contradicted_claim_rate": _rate(bad_claims, claims),
        "reviewer_resolution_rate": _rate(resolved_issues, valid_issues),
        "fault_recovery_rate": _rate(sum(item["recovered"] for item in faults), len(faults)),
        "faults_triggered": _rate(sum(item["fault_triggered"] for item in faults), len(faults)),
        "cost": {
            key: _distribution([item["metrics"][key] for item in present])
            for key in ("model_calls", "input_tokens", "output_tokens", "latency_ms")
        },
    }


def evaluate_records(
    runs: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    gold_records: Sequence[Mapping[str, Any]],
    overlays: Sequence[Mapping[str, Any]],
    *,
    mode: str = "development",
    official_data_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if mode not in {"development", "publish"}:
        raise EvaluationError(f"unknown mode: {mode}")
    case_by_id = _index_unique(cases, "case_id", "cases")
    gold_by_id = _index_unique(gold_records, "case_id", "gold")
    overlay_by_id = _index_unique(overlays, "overlay_id", "fault overlays")
    if set(case_by_id) != set(gold_by_id):
        raise EvaluationError("case/gold foreign keys do not match")
    for case_id in sorted(case_by_id):
        validate_case_gold_contract(
            case_by_id[case_id], gold_by_id[case_id], f"cases[{case_id}]"
        )
    if any(_text(item.get("case_id")) not in case_by_id for item in overlays):
        raise EvaluationError("a fault overlay refers to an unknown case")
    for overlay_id, overlay in sorted(overlay_by_id.items()):
        case_id = _text(overlay.get("case_id"))
        validate_overlay_contract(
            overlay, case_by_id[case_id], f"fault_overlays[{overlay_id}]"
        )
    normal_index, fault_index = _index_runs(runs, case_by_id, overlay_by_id)
    if mode == "publish":
        if dict(official_data_hashes or {}) != OFFICIAL_DATA_SHA256:
            raise EvaluationError(
                "publish mode requires the pinned official cases/gold/overlays hashes"
            )
        validate_publish_bundle(
            runs,
            cases,
            gold_records,
            overlays,
            official_data_hashes=official_data_hashes or {},
        )
    normal_detail: dict[str, list[dict[str, Any]]] = {variant: [] for variant in VARIANTS}
    fault_detail: dict[str, list[dict[str, Any]]] = {variant: [] for variant in VARIANTS}
    for variant in VARIANTS:
        for case_id in sorted(case_by_id):
            run = normal_index.get((variant, case_id))
            score = score_run(run, case_by_id[case_id], gold_by_id[case_id]) if run else _missing_run_score()
            normal_detail[variant].append({"variant": variant, "case_id": case_id, "score": score})
        for overlay_id in sorted(overlay_by_id):
            overlay = overlay_by_id[overlay_id]
            case_id = _text(overlay.get("case_id"))
            score = score_fault_run(
                fault_index.get((variant, overlay_id)), overlay, case_by_id[case_id], gold_by_id[case_id]
            )
            fault_detail[variant].append(
                {
                    "variant": variant,
                    "case_id": case_id,
                    "overlay_id": overlay_id,
                    "fault_type": overlay.get("fault_type"),
                    "score": score,
                }
            )
    labels = PUBLISH_LABELS if mode == "publish" else DEVELOPMENT_LABELS
    normal_labels = PUBLISH_NORMAL_LABELS if mode == "publish" else DEVELOPMENT_LABELS
    fault_labels = PUBLISH_FAULT_LABELS if mode == "publish" else DEVELOPMENT_LABELS
    for variant in VARIANTS:
        for item in normal_detail[variant]:
            item["labels"] = list(normal_labels)
        for item in fault_detail[variant]:
            item["labels"] = list(fault_labels)
    summary = {
        "schema_version": "2.0",
        "mode": mode,
        "complete": mode == "publish",
        "labels": labels,
        "evidence_labels": {
            "normal_runs": list(normal_labels),
            "fault_runs": list(fault_labels),
        },
        "scorer_version": SCORER_VERSION,
        "methodology": {
            "plan_executable_scope": (
                "agent_choices_plus_shared_deterministic_argument_binding"
            ),
            "normal_runs": "live_synthetic_single_run" if mode == "publish" else "development",
            "fault_runs": (
                "deterministic_replay_fault_injection"
                if mode == "publish"
                else "development"
            ),
        },
        "expected": {
            "cases": len(case_by_id),
            "normal_runs_per_variant": len(case_by_id),
            "fault_overlays": len(overlay_by_id),
            "fault_runs_per_variant": len(overlay_by_id),
        },
        "variants": {
            variant: _aggregate_variant(
                [item["score"] for item in normal_detail[variant]],
                [item["score"] for item in fault_detail[variant]],
            )
            for variant in VARIANTS
        },
    }
    return {"summary": summary, "normal_detail": normal_detail, "fault_detail": fault_detail}


def _validate_publish_model(provenance: Mapping[str, Any], location: str) -> None:
    requested = _required_text(provenance.get("requested_model"), f"{location}.requested_model")
    actual = _required_text(provenance.get("actual_model"), f"{location}.actual_model")
    if requested != REQUESTED_MODEL:
        raise EvaluationError(f"{location}.requested_model must be {REQUESTED_MODEL}")
    if provenance.get("model") != actual:
        raise EvaluationError(f"{location}.model must equal actual_model")
    reason = provenance.get("fallback_reason")
    if actual == REQUESTED_MODEL and reason not in {None, ""}:
        raise EvaluationError(f"{location}.fallback_reason must be empty without fallback")
    if actual == FALLBACK_MODEL and reason != FALLBACK_REASON:
        raise EvaluationError(f"{location}.fallback_reason must be {FALLBACK_REASON}")
    if actual not in {REQUESTED_MODEL, FALLBACK_MODEL}:
        raise EvaluationError(f"{location}.actual_model is not an allowed model")


def validate_publish_bundle(
    runs: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    gold_records: Sequence[Mapping[str, Any]],
    overlays: Sequence[Mapping[str, Any]],
    *,
    official_data_hashes: Mapping[str, str],
) -> None:
    if dict(official_data_hashes) != OFFICIAL_DATA_SHA256:
        raise EvaluationError("publish official dataset hashes do not match pinned values")
    record_sets = {
        "cases": cases,
        "gold": gold_records,
        "overlays": overlays,
    }
    for label, records in record_sets.items():
        if _hash_records(records) != OFFICIAL_RECORD_SHA256[label]:
            raise EvaluationError(
                f"publish {label} records do not match the pinned official content"
            )
    if len(cases) != OFFICIAL_CASE_COUNT or len(gold_records) != OFFICIAL_CASE_COUNT:
        raise EvaluationError("publish mode requires the official 24 case/gold records")
    if len(overlays) != OFFICIAL_FAULT_COUNT:
        raise EvaluationError("publish mode requires the official 12 fault overlays")
    case_ids = {_text(item.get("case_id")) for item in cases}
    overlay_ids = {_text(item.get("overlay_id")) for item in overlays}
    expected_normal = {(variant, case_id) for variant in VARIANTS for case_id in case_ids}
    expected_fault = {(variant, overlay_id) for variant in VARIANTS for overlay_id in overlay_ids}
    normal: dict[tuple[str, str], Mapping[str, Any]] = {}
    normal_by_run_id: dict[str, Mapping[str, Any]] = {}
    faults: dict[tuple[str, str], Mapping[str, Any]] = {}
    overlay_by_id = {_text(item.get("overlay_id")): item for item in overlays}
    case_by_id = {_text(item.get("case_id")): item for item in cases}
    for run in runs:
        provenance = _dict(run.get("provenance"))
        if provenance.get("provider") != "dashscope":
            raise EvaluationError("publish mode requires provider=dashscope")
        case_id = _text(run.get("case_id"))
        if provenance.get("data_hash") != canonical_record_hash(case_by_id[case_id]):
            raise EvaluationError(
                "publish run data_hash does not match its canonical case object"
            )
        if not PROMPT_ROLES.issubset(set(_dict(provenance.get("prompt_hashes")))):
            raise EvaluationError("publish provenance must include all four prompt hashes")
        _validate_publish_model(provenance, f"run {_text(run.get('run_id'))}.provenance")
        variant = _text(run.get("variant"))
        if run.get("run_kind") == "evaluation":
            if provenance.get("execution_mode") != "live":
                raise EvaluationError("all 48 normal publish runs must be live")
            key = (variant, _text(run.get("case_id")))
            normal[key] = run
            normal_by_run_id[_text(run.get("run_id"))] = run
        else:
            if provenance.get("execution_mode") != "replay":
                raise EvaluationError("all 24 fault publish runs must be replay")
            overlay_id = _text(_dict(run.get("fault")).get("overlay_id"))
            faults[(variant, overlay_id)] = run
    if set(normal) != expected_normal:
        raise EvaluationError("publish mode requires exactly 48 normal run identities")
    if set(faults) != expected_fault:
        raise EvaluationError("publish mode requires exactly 24 fault run identities")
    if len(runs) != len(expected_normal) + len(expected_fault):
        raise EvaluationError("publish mode rejects extra or duplicate runs")
    actual_models = {
        _text(_dict(run.get("provenance")).get("actual_model"))
        for run in normal.values()
    }
    if len(actual_models) != 1:
        raise EvaluationError("all 48 paired normal runs must use one actual model")
    prompt_sets_all = {
        json.dumps(
            _dict(_dict(run.get("provenance")).get("prompt_hashes")),
            sort_keys=True,
            separators=(",", ":"),
        )
        for run in normal.values()
    }
    if len(prompt_sets_all) != 1:
        raise EvaluationError("all 48 normal runs must use one prompt hash set")
    for variant in VARIANTS:
        variant_runs = [run for (item_variant, _), run in normal.items() if item_variant == variant]
        if len({_text(_dict(run.get("provenance")).get("config_hash")) for run in variant_runs}) != 1:
            raise EvaluationError(f"{variant} normal runs must use one config_hash")
    for (variant, overlay_id), run in faults.items():
        provenance = _dict(run.get("provenance"))
        source_id = _required_text(
            provenance.get("source_run_id"),
            f"fault run {_text(run.get('run_id'))}.provenance.source_run_id",
        )
        source = normal_by_run_id.get(source_id)
        overlay = overlay_by_id[overlay_id]
        if source is None:
            raise EvaluationError("fault replay source_run_id must reference a live normal run")
        if source.get("variant") != variant or source.get("case_id") != overlay.get("case_id"):
            raise EvaluationError("fault replay source must match case and variant")
        if provenance.get("actual_model") != _dict(source.get("provenance")).get("actual_model"):
            raise EvaluationError("fault replay actual_model must match its live source")
        source_provenance = _dict(source.get("provenance"))
        for field in ("config_hash", "data_hash"):
            if provenance.get(field) != source_provenance.get(field):
                raise EvaluationError(f"fault replay {field} must match its live source")
        if provenance.get("prompt_hashes") != source_provenance.get("prompt_hashes"):
            raise EvaluationError("fault replay prompt_hashes must match its live source")


CSV_FIELDS = [
    "labels", "row_type", "variant", "case_id", "overlay_id", "fault_type",
    "present", "status", "plan_executable", "invalid_steps", "total_steps",
    "invalid_step_rate", "missing_required_checks", "missing_required_evidence_tags",
    "task_completed", "top1_root_cause_id", "top1_hit", "bad_claims",
    "auditable_claims", "valid_review_issues", "resolved_review_issues",
    "fault_triggered", "fault_recovered", "model_calls", "input_tokens",
    "output_tokens", "latency_ms", "failures",
]


def _csv_rows(report: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    for variant in VARIANTS:
        for item in report["normal_detail"][variant]:
            score = item["score"]
            steps = score["plan"]["total_step_count"]
            yield {
                "labels": "/".join(item["labels"]),
                "row_type": "evaluation",
                "variant": variant,
                "case_id": item["case_id"],
                "overlay_id": "",
                "fault_type": "",
                "present": score["present"],
                "status": score["status"],
                "plan_executable": score["plan"]["executable"],
                "invalid_steps": score["plan"]["invalid_step_count"],
                "total_steps": steps,
                "invalid_step_rate": round(score["plan"]["invalid_step_count"] / steps, 6) if steps else "N/A",
                "missing_required_checks": ";".join(score["plan"]["missing_required_checks"]),
                "missing_required_evidence_tags": ";".join(score["missing_required_evidence_tags"]),
                "task_completed": score["task_completed"],
                "top1_root_cause_id": score["top1_root_cause_id"],
                "top1_hit": score["top1_hit"],
                "bad_claims": score["bad_claim_count"],
                "auditable_claims": score["auditable_claim_count"],
                "valid_review_issues": score["review"]["valid_count"],
                "resolved_review_issues": score["review"]["resolved_count"],
                "fault_triggered": "",
                "fault_recovered": "",
                "model_calls": score["metrics"]["model_calls"],
                "input_tokens": score["metrics"]["input_tokens"],
                "output_tokens": score["metrics"]["output_tokens"],
                "latency_ms": score["metrics"]["latency_ms"],
                "failures": ";".join(score["failures"]),
            }
        for item in report["fault_detail"][variant]:
            fault_score = item["score"]
            score = fault_score["run_score"]
            steps = score["plan"]["total_step_count"]
            yield {
                "labels": "/".join(item["labels"]),
                "row_type": "fault_injection",
                "variant": variant,
                "case_id": item["case_id"],
                "overlay_id": item["overlay_id"],
                "fault_type": item["fault_type"],
                "present": fault_score["present"],
                "status": score["status"],
                "plan_executable": score["plan"]["executable"],
                "invalid_steps": score["plan"]["invalid_step_count"],
                "total_steps": steps,
                "invalid_step_rate": round(score["plan"]["invalid_step_count"] / steps, 6) if steps else "N/A",
                "missing_required_checks": ";".join(score["plan"]["missing_required_checks"]),
                "missing_required_evidence_tags": ";".join(score["missing_required_evidence_tags"]),
                "task_completed": score["task_completed"],
                "top1_root_cause_id": score["top1_root_cause_id"],
                "top1_hit": score["top1_hit"],
                "bad_claims": score["bad_claim_count"],
                "auditable_claims": score["auditable_claim_count"],
                "valid_review_issues": score["review"]["valid_count"],
                "resolved_review_issues": score["review"]["resolved_count"],
                "fault_triggered": fault_score["fault_triggered"],
                "fault_recovered": fault_score["recovered"],
                "model_calls": score["metrics"]["model_calls"],
                "input_tokens": score["metrics"]["input_tokens"],
                "output_tokens": score["metrics"]["output_tokens"],
                "latency_ms": score["metrics"]["latency_ms"],
                "failures": ";".join(fault_score["failures"]),
            }


def render_csv(report: Mapping[str, Any]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(_csv_rows(report))
    return buffer.getvalue()


def render_badcases(report: Mapping[str, Any]) -> str:
    lines = [
        "# Evaluation badcases",
        "",
        "Labels: " + " / ".join(report["summary"]["labels"]),
        "",
        "Only deterministic failures are listed. This document is not an expert review.",
        "",
        "| Type | Variant | Case | Overlay | Failures |",
        "|---|---|---|---|---|",
    ]
    count = 0
    for row in _csv_rows(report):
        if not row["failures"]:
            continue
        count += 1
        values = [
            str(row["row_type"]), str(row["variant"]), str(row["case_id"]),
            str(row["overlay_id"] or "-"), str(row["failures"]).replace("|", "\\|"),
        ]
        lines.append("| " + " | ".join(values) + " |")
    if not count:
        lines.append("| - | - | - | - | No deterministic badcase detected |")
    lines.append("")
    return "\n".join(lines)


def _provenance_manifest(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    keys = {
        "execution_modes": "execution_mode",
        "providers": "provider",
        "models": "model",
        "requested_models": "requested_model",
        "actual_models": "actual_model",
        "fallback_reasons": "fallback_reason",
        "config_hashes": "config_hash",
        "data_hashes": "data_hash",
        "source_run_ids": "source_run_id",
    }
    values: dict[str, set[str]] = {key: set() for key in keys}
    prompt_hashes: dict[str, set[str]] = defaultdict(set)
    run_ids: set[str] = set()
    for run in runs:
        run_ids.add(_text(run.get("run_id")))
        provenance = _dict(run.get("provenance"))
        for output, field in keys.items():
            if value := _text(provenance.get(field)):
                values[output].add(value)
        for role, digest in _dict(provenance.get("prompt_hashes")).items():
            if _text(role) and _text(digest):
                prompt_hashes[str(role)].add(str(digest))
    return {
        **{key: sorted(items) for key, items in values.items()},
        "prompt_hashes": {role: sorted(items) for role, items in sorted(prompt_hashes.items())},
        "run_ids": sorted(item for item in run_ids if item),
    }


def build_manifest(
    runs: Sequence[Mapping[str, Any]],
    cases_path: str | Path,
    gold_path: str | Path,
    faults_path: str | Path,
    runs_path: str | Path,
    *,
    mode: str,
    labels: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "mode": mode,
        "complete": mode == "publish",
        "labels": list(labels),
        "scorer_version": SCORER_VERSION,
        "official_dataset_sha256": dict(OFFICIAL_DATA_SHA256),
        "official_record_content_sha256": dict(OFFICIAL_RECORD_SHA256),
        "run_data_hash_semantics": "sha256_of_each_case_canonical_json_object",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "record_counts": {
            "runs": len(runs),
            "evaluation_runs": sum(item.get("run_kind") == "evaluation" for item in runs),
            "fault_injection_runs": sum(item.get("run_kind") == "fault_injection" for item in runs),
        },
        "sha256": {
            "cases.jsonl": _hash_file(cases_path),
            "gold.jsonl": _hash_file(gold_path),
            "overlays.jsonl": _hash_file(faults_path),
            "runs.jsonl": _hash_file(runs_path),
        },
        "provenance": _provenance_manifest(runs),
        "path_policy": "hashes_and_basenames_only_no_absolute_paths",
    }


def write_reports(
    report: Mapping[str, Any], manifest: Mapping[str, Any], output_dir: str | Path
) -> None:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (destination / "summary.json").write_text(
        json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (destination / "per_case.csv").write_text(render_csv(report), encoding="utf-8")
    (destination / "badcases.md").write_text(render_badcases(report), encoding="utf-8")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("development", "publish"), required=True)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--faults", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def _official_data_hashes(args: argparse.Namespace) -> dict[str, str]:
    root = Path(__file__).resolve().parent / "data"
    canonical = {
        "cases": root / "frozen" / "cases.jsonl",
        "gold": root / "frozen" / "gold.jsonl",
        "faults": root / "faults" / "overlays.jsonl",
    }
    supplied = {"cases": args.cases, "gold": args.gold, "faults": args.faults}
    pinned_keys = {"cases": "cases", "gold": "gold", "faults": "overlays"}
    for input_key, pinned_key in pinned_keys.items():
        pinned = OFFICIAL_DATA_SHA256[pinned_key]
        if _hash_file(canonical[input_key]) != pinned:
            raise EvaluationError(
                f"committed official {input_key} drifted from its pinned SHA-256"
            )
        if _hash_file(supplied[input_key]) != pinned:
            raise EvaluationError(
                f"publish {input_key} does not match its pinned official SHA-256"
            )
    return dict(OFFICIAL_DATA_SHA256)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        runs = load_jsonl(args.runs)
        cases = load_jsonl(args.cases)
        gold = load_jsonl(args.gold)
        overlays = load_jsonl(args.faults)
        official_data_hashes = (
            _official_data_hashes(args) if args.mode == "publish" else None
        )
        report = evaluate_records(
            runs,
            cases,
            gold,
            overlays,
            mode=args.mode,
            official_data_hashes=official_data_hashes,
        )
        manifest = build_manifest(
            runs, args.cases, args.gold, args.faults, args.runs,
            mode=args.mode, labels=report["summary"]["labels"],
        )
    except (EvaluationError, OSError) as exc:
        print(f"evaluation input rejected: {exc}", file=sys.stderr)
        return 2
    # All gates complete before the first output write.
    write_reports(report, manifest, args.output_dir)
    print(
        json.dumps(
            {
                "mode": args.mode,
                "complete": args.mode == "publish",
                "labels": report["summary"]["labels"],
                "output_files": ["manifest.json", "per_case.csv", "summary.json", "badcases.md"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
