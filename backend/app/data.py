from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import EmiCase


class CaseNotFoundError(KeyError):
    pass


class CaseCatalog:
    def __init__(self, path: Path):
        self.path = path
        self._cases: dict[str, EmiCase] = {}

    def load(self) -> None:
        cases: dict[str, EmiCase] = {}
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    raw_case = json.loads(line)
                    if not isinstance(raw_case, dict):
                        raise ValueError("case must be a JSON object")
                    raw_case.setdefault("title", raw_case.get("case_id", "untitled case"))
                    raw_case.setdefault("category", "unlabeled")
                    raw_case.setdefault(
                        "symptom",
                        json.dumps(
                            raw_case.get("observations", "synthetic observation set"),
                            ensure_ascii=False,
                            sort_keys=True,
                        )[:2000],
                    )
                    case = EmiCase.model_validate(raw_case)
                except Exception as exc:  # pragma: no cover - exact pydantic text can drift
                    raise ValueError(f"invalid case at line {line_number}") from exc
                if case.case_id in cases:
                    raise ValueError(f"duplicate case_id: {case.case_id}")
                cases[case.case_id] = case
        if not cases:
            raise ValueError("case catalog is empty")
        self._cases = cases

    def list_public(self) -> list[dict[str, Any]]:
        return [
            {
                "case_id": case.case_id,
                "title": case.title,
                "category": case.category,
                "symptom": case.symptom,
                "context": case.context,
                "constraints": case.constraints,
            }
            for case in sorted(self._cases.values(), key=lambda item: item.case_id)
        ]

    def get(self, case_id: str) -> dict[str, Any]:
        try:
            return self._cases[case_id].model_dump(mode="json")
        except KeyError as exc:
            raise CaseNotFoundError(case_id) from exc

    def get_for_execution(self, case_id: str) -> dict[str, Any]:
        return sanitize_case_for_execution(self.get(case_id))


def sanitize_case_for_execution(case: dict[str, Any]) -> dict[str, Any]:
    """Remove split labels and classification fields before any provider sees the case."""

    safe = json.loads(json.dumps(case, ensure_ascii=False, sort_keys=True))
    safe.pop("category", None)
    safe.pop("split", None)
    safe.pop("schema_version", None)
    return safe


def project_case_for_provider(case: dict[str, Any]) -> dict[str, Any]:
    """Return only non-answer case description; source selectors stay tool-side."""

    safe = sanitize_case_for_execution(case)
    return {
        "title": safe.get("title", ""),
        "symptom": safe.get("symptom", ""),
        "context": _redact_provider_context(safe.get("context", {})),
        "constraints": _redact_provider_context(safe.get("constraints", [])),
    }


def _redact_provider_context(value: Any) -> Any:
    blocked = {
        "id",
        "source_id",
        "observation_id",
        "intervention_id",
        "path_id",
        "arguments",
        "payload",
        "evidence_tags",
        "required_checks",
        "tool_data",
        "observations",
        "measurements",
        "interventions",
        "coupling_paths",
    }
    if isinstance(value, dict):
        return {
            key: _redact_provider_context(item)
            for key, item in value.items()
            if key not in blocked and not key.endswith("_id")
        }
    if isinstance(value, list):
        return [_redact_provider_context(item) for item in value]
    return value


def load_json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path.name}")
    return value
