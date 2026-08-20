from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ToolName = Literal[
    "query_measurement",
    "match_frequency_signature",
    "compare_intervention",
    "inspect_coupling_path",
    "check_measurement_consistency",
]
ProfileName = Literal["baseline", "optimized"]
ProviderName = Literal["fixture", "dashscope"]
RootCauseId = Literal[
    "power_input_filter_resonance",
    "shared_return_impedance",
    "shield_pigtail_inductance",
    "clock_harmonic_radiation",
    "interface_filter_placement",
    "near_field_inductive_coupling",
]
ROOT_CAUSE_TAXONOMY: dict[RootCauseId, str] = {
    "power_input_filter_resonance": "电源输入滤波谐振",
    "shared_return_impedance": "共享回流阻抗",
    "shield_pigtail_inductance": "屏蔽尾辫电感",
    "clock_harmonic_radiation": "时钟谐波辐射",
    "interface_filter_placement": "接口滤波器布置",
    "near_field_inductive_coupling": "近场感性耦合",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmiCase(StrictModel):
    schema_version: str | None = None
    split: str | None = None
    case_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80)
    symptom: str = Field(min_length=1, max_length=2000)
    context: dict[str, Any] = Field(default_factory=dict)
    measurements: dict[str, Any] = Field(default_factory=dict)
    interventions: list[dict[str, Any]] = Field(default_factory=list)
    coupling_paths: list[dict[str, Any]] = Field(default_factory=list)
    observations: dict[str, Any] | list[Any] = Field(default_factory=dict)
    constraints: dict[str, Any] | list[Any] = Field(default_factory=dict)
    required_checks: dict[str, Any] | list[Any] = Field(default_factory=list)
    tool_data: list[dict[str, Any]] = Field(default_factory=list)


class Hypothesis(StrictModel):
    hypothesis_id: str = Field(min_length=1, max_length=80)
    statement: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=1000)


class PlanStep(StrictModel):
    step_id: str = Field(min_length=1, max_length=80)
    hypothesis_id: str = Field(min_length=1, max_length=80)
    tool: ToolName
    arguments: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list)
    completion_condition: str = Field(min_length=1, max_length=500)


class PlanOutput(StrictModel):
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=8)
    information_gaps: list[str] = Field(default_factory=list, max_length=12)
    steps: list[PlanStep] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_identifiers(self) -> "PlanOutput":
        hypothesis_ids = [item.hypothesis_id for item in self.hypotheses]
        step_ids = [item.step_id for item in self.steps]
        if len(hypothesis_ids) != len(set(hypothesis_ids)):
            raise ValueError("hypothesis_id values must be unique")
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step_id values must be unique")
        known_steps = set(step_ids)
        known_hypotheses = set(hypothesis_ids)
        for step in self.steps:
            if step.hypothesis_id not in known_hypotheses:
                raise ValueError(f"unknown hypothesis_id: {step.hypothesis_id}")
            if step.step_id in step.depends_on:
                raise ValueError(f"step {step.step_id} depends on itself")
            unknown = set(step.depends_on) - known_steps
            if unknown:
                raise ValueError(f"unknown dependencies: {sorted(unknown)}")
        return self


class ToolInvocation(StrictModel):
    tool: ToolName
    arguments: dict[str, Any]


class EvidenceRecord(StrictModel):
    operation_id: str
    evidence_id: str
    step_id: str
    hypothesis_id: str
    tool: ToolName
    status: Literal["success", "failure"]
    phase: Literal["initial", "rework"]
    observations: list[str] = Field(default_factory=list)
    supports_claim_ids: list[str] = Field(default_factory=list)
    contradicts_claim_ids: list[str] = Field(default_factory=list)
    evidence_tags: list[str] = Field(default_factory=list)
    started_at: str
    finished_at: str
    attempt: int = Field(ge=1)
    error: str | None = None


class RootCause(StrictModel):
    cause_id: RootCauseId
    label: str
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str


class AtomicClaim(StrictModel):
    claim_id: str
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    contradicted_by: list[str] = Field(default_factory=list)
    support_status: Literal["supported", "unsupported", "contradicted"]


class DiagnosisOutput(StrictModel):
    root_causes: list[RootCause] = Field(min_length=1, max_length=8)
    claims: list[AtomicClaim] = Field(min_length=1, max_length=16)
    confidence_boundary: str
    status: Literal["complete", "insufficient_evidence"]

    @field_validator("root_causes")
    @classmethod
    def order_root_causes(cls, value: list[RootCause]) -> list[RootCause]:
        return sorted(value, key=lambda item: (-item.confidence, item.cause_id))


class ReviewIssue(StrictModel):
    issue_id: str
    kind: Literal[
        "unsupported_claim",
        "contradicted_claim",
        "failed_step",
        "plan_gap",
        "invalid_step",
    ]
    severity: Literal["low", "medium", "high"]
    step_id: str | None = None
    claim_id: str | None = None
    message: str
    resolved: bool = False
    required_tool: ToolName | None = None
    required_arguments: dict[str, Any] = Field(default_factory=dict)


class ReviewOutput(StrictModel):
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=16)
    needs_rework: bool


class ModelUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ProviderResponse(StrictModel):
    data: dict[str, Any]
    usage: ModelUsage = Field(default_factory=ModelUsage)
    model: str
    inference_config: dict[str, Any]


class RunRequest(StrictModel):
    case_id: str
    profile: ProfileName = "optimized"
    provider: ProviderName = "fixture"
    run_kind: Literal["demo", "evaluation", "fault_injection"] = "demo"
    overlay_id: str | None = None
    fault_type: str | None = None


class DecisionRequest(StrictModel):
    decision: Literal["accepted", "rejected", "modified"]
    selected_cause_id: str | None = None
    notes: str = Field(default="", max_length=2000)


class RunRecord(StrictModel):
    run_id: str
    thread_id: str
    attempt_id: str
    case_id: str
    profile: ProfileName
    provider: ProviderName
    run_kind: Literal["demo", "evaluation", "fault_injection"] = "demo"
    overlay_id: str | None = None
    fault_type: str | None = None
    status: str
    created_at: str
    updated_at: str
    error: str | None = None
    decision: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
