from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .contracts import ToolName


class ToolExecutionError(RuntimeError):
    def __init__(self, code: str, *, transient: bool = False):
        super().__init__(code)
        self.code = code
        self.transient = transient


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryMeasurementInput(ToolInput):
    key: str


class FrequencySignatureInput(ToolInput):
    fundamental_mhz: float = Field(gt=0)
    peak_key: str = "spectrum_peaks_mhz"
    tolerance_mhz: float = Field(default=0.5, gt=0, le=5)


class InterventionInput(ToolInput):
    intervention_id: str


class CouplingPathInput(ToolInput):
    path_id: str


class ConsistencyInput(ToolInput):
    measurement_key: str
    tolerance_percent: float = Field(default=10, gt=0, le=100)


ToolFunction = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


def _unwrap_evidence(value: Any) -> tuple[Any, list[str]]:
    if not isinstance(value, dict):
        return value, []
    tags = [str(item) for item in value.get("evidence_tags", [])]
    if "value" in value:
        return value["value"], tags
    if "values" in value:
        return value["values"], tags
    return {key: item for key, item in value.items() if key != "evidence_tags"}, tags


def _find_observation(case: dict[str, Any], key: str) -> Any | None:
    observations = case.get("observations", {})
    if isinstance(observations, dict):
        return observations.get(key)
    if isinstance(observations, list):
        return next(
            (
                item
                for item in observations
                if isinstance(item, dict)
                and (item.get("observation_id") or item.get("id") or item.get("key")) == key
            ),
            None,
        )
    return None


def _find_tool_data(
    case: dict[str, Any], tool: ToolName, arguments: dict[str, Any]
) -> dict[str, Any] | None:
    for item in case.get("tool_data", []):
        if not isinstance(item, dict) or item.get("tool") != tool:
            continue
        configured = item.get("arguments", {})
        if isinstance(configured, dict) and configured == arguments:
            return item
    return None


def _tool_payload(
    case: dict[str, Any], tool: ToolName, arguments: dict[str, Any]
) -> tuple[Any | None, list[str]]:
    item = _find_tool_data(case, tool, arguments)
    if item is None:
        return None, []
    payload, nested_tags = _unwrap_evidence(item.get("payload"))
    explicit_tags = [str(value) for value in item.get("evidence_tags", [])]
    return payload, sorted(set(nested_tags + explicit_tags))


async def query_measurement(case: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    params = QueryMeasurementInput.model_validate(arguments)
    tool_payload, tool_tags = _tool_payload(case, "query_measurement", arguments)
    measurements = case.get("measurements", {})
    if tool_payload is not None:
        raw_value = tool_payload
    elif params.key in measurements:
        raw_value = measurements[params.key]
    elif (raw_value := _find_observation(case, params.key)) is not None:
        pass
    else:
        raise ToolExecutionError("evidence_not_found")
    value, evidence_tags = _unwrap_evidence(raw_value)
    evidence_tags = sorted(set(evidence_tags + tool_tags))
    return {
        "observations": [f"measurement {params.key}={value}"],
        "supports": [],
        "contradicts": [],
        "evidence_tags": evidence_tags,
        "data": {"key": params.key, "value": value},
    }


async def match_frequency_signature(
    case: dict[str, Any], arguments: dict[str, Any]
) -> dict[str, Any]:
    params = FrequencySignatureInput.model_validate(arguments)
    tool_payload, tool_tags = _tool_payload(case, "match_frequency_signature", arguments)
    source = (
        tool_payload
        if tool_payload is not None
        else case.get("measurements", {}).get(params.peak_key)
    )
    peaks, evidence_tags = _unwrap_evidence(source)
    evidence_tags = sorted(set(evidence_tags + tool_tags))
    if isinstance(peaks, dict) and "matched" in peaks:
        matched = bool(peaks.get("matched"))
        harmonic_order = peaks.get("harmonic_order")
        delta_mhz = peaks.get("delta_mhz")
        return {
            "observations": [
                f"frequency signature {params.peak_key}: matched={matched}; "
                f"harmonic_order={harmonic_order}; delta_mhz={delta_mhz}"
            ],
            "supports": ["frequency_harmonic_alignment"] if matched else [],
            "contradicts": [] if matched else ["frequency_harmonic_alignment"],
            "evidence_tags": evidence_tags,
            "data": peaks,
        }
    if not isinstance(peaks, list) or not peaks:
        raise ToolExecutionError("frequency_peaks_missing")
    numeric_peaks = [float(item) for item in peaks]
    matches: list[dict[str, float | int]] = []
    for peak in numeric_peaks:
        harmonic = max(1, round(peak / params.fundamental_mhz))
        expected = harmonic * params.fundamental_mhz
        error = abs(peak - expected)
        if error <= params.tolerance_mhz:
            matches.append(
                {
                    "peak_mhz": peak,
                    "harmonic": harmonic,
                    "error_mhz": round(error, 4),
                }
            )
    observation = (
        f"matched {len(matches)}/{len(numeric_peaks)} peaks to harmonics of "
        f"{params.fundamental_mhz} MHz"
    )
    return {
        "observations": [observation],
        "supports": ["frequency_harmonic_alignment"] if matches else [],
        "contradicts": [] if matches else ["frequency_harmonic_alignment"],
        "evidence_tags": evidence_tags,
        "data": {"matches": matches, "peak_count": len(numeric_peaks)},
    }


async def compare_intervention(case: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    params = InterventionInput.model_validate(arguments)
    tool_payload, tool_tags = _tool_payload(case, "compare_intervention", arguments)
    intervention = tool_payload if isinstance(tool_payload, dict) else next(
        (
            item
            for item in case.get("interventions", [])
            if item.get("intervention_id") == params.intervention_id
        ),
        None,
    )
    if intervention is None:
        raise ToolExecutionError("intervention_not_found")
    before = float(intervention["before_dbuv"])
    after = float(intervention["after_dbuv"])
    delta = round(after - before, 3)
    evidence_tags = sorted(
        set(tool_tags + [str(item) for item in intervention.get("evidence_tags", [])])
    )
    return {
        "observations": [
            f"intervention {params.intervention_id}: {before} dBuV -> {after} dBuV "
            f"(delta {delta} dB)"
        ],
        "supports": [str(intervention.get("tests_hypothesis", "intervention_effect"))]
        if delta <= -3
        else [],
        "evidence_tags": evidence_tags,
        "contradicts": [str(intervention.get("tests_hypothesis", "intervention_effect"))]
        if delta > -3
        else [],
        "data": {"before_dbuv": before, "after_dbuv": after, "delta_db": delta},
    }


async def inspect_coupling_path(case: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    params = CouplingPathInput.model_validate(arguments)
    tool_payload, tool_tags = _tool_payload(case, "inspect_coupling_path", arguments)
    path = tool_payload if isinstance(tool_payload, dict) else next(
        (item for item in case.get("coupling_paths", []) if item.get("path_id") == params.path_id),
        None,
    )
    if path is None:
        raise ToolExecutionError("coupling_path_not_found")
    path_status = str(path.get("path_status", ""))
    plausible = bool(
        path.get("plausible", path_status in {"traceable", "plausible", "confirmed"})
    )
    reason = str(path.get("observation", path_status or "no observation supplied"))
    evidence_tags = sorted(
        set(tool_tags + [str(item) for item in path.get("evidence_tags", [])])
    )
    return {
        "observations": [f"coupling path {params.path_id}: plausible={plausible}; {reason}"],
        "supports": [str(path.get("hypothesis", params.path_id))] if plausible else [],
        "contradicts": [] if plausible else [str(path.get("hypothesis", params.path_id))],
        "evidence_tags": evidence_tags,
        "data": {"plausible": plausible},
    }


async def check_measurement_consistency(
    case: dict[str, Any], arguments: dict[str, Any]
) -> dict[str, Any]:
    params = ConsistencyInput.model_validate(arguments)
    tool_payload, tool_tags = _tool_payload(
        case, "check_measurement_consistency", arguments
    )
    source = (
        tool_payload
        if tool_payload is not None
        else case.get("measurements", {}).get(params.measurement_key)
    )
    values, evidence_tags = _unwrap_evidence(source)
    evidence_tags = sorted(set(evidence_tags + tool_tags))
    if isinstance(values, dict) and "consistent" in values:
        consistent = bool(values.get("consistent"))
        spread_percent = float(values.get("max_delta_percent", 0))
        return {
            "observations": [
                f"measurement {params.measurement_key}: spread={spread_percent:.2f}% "
                f"within_tolerance={consistent}"
            ],
            "supports": ["measurement_consistent"] if consistent else [],
            "contradicts": [] if consistent else ["measurement_consistent"],
            "evidence_tags": evidence_tags,
            "data": values,
        }
    if not isinstance(values, list) or len(values) < 2:
        raise ToolExecutionError("repeated_measurements_missing")
    numeric = [float(item) for item in values]
    mean = sum(numeric) / len(numeric)
    if math.isclose(mean, 0):
        spread_percent = 0.0 if max(numeric) == min(numeric) else 1_000_000.0
    else:
        spread_percent = (max(numeric) - min(numeric)) / abs(mean) * 100
    consistent = spread_percent <= params.tolerance_percent
    return {
        "observations": [
            f"measurement {params.measurement_key}: spread={spread_percent:.2f}% "
            f"within_tolerance={consistent}"
        ],
        "supports": ["measurement_consistent"] if consistent else [],
        "contradicts": [] if consistent else ["measurement_consistent"],
        "evidence_tags": evidence_tags,
        "data": {"spread_percent": round(spread_percent, 4), "consistent": consistent},
    }


TOOL_REGISTRY: dict[ToolName, ToolFunction] = {
    "query_measurement": query_measurement,
    "match_frequency_signature": match_frequency_signature,
    "compare_intervention": compare_intervention,
    "inspect_coupling_path": inspect_coupling_path,
    "check_measurement_consistency": check_measurement_consistency,
}

TOOL_INPUT_MODELS: dict[ToolName, type[ToolInput]] = {
    "query_measurement": QueryMeasurementInput,
    "match_frequency_signature": FrequencySignatureInput,
    "compare_intervention": InterventionInput,
    "inspect_coupling_path": CouplingPathInput,
    "check_measurement_consistency": ConsistencyInput,
}


def validate_tool_arguments(tool: ToolName, arguments: dict[str, Any]) -> None:
    TOOL_INPUT_MODELS[tool].model_validate(arguments)


async def execute_tool(
    tool: ToolName,
    case: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return await TOOL_REGISTRY[tool](case, arguments)
