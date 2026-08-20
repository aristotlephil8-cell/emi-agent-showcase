from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evaluation.scoring import (
    DEVELOPMENT_LABELS,
    EvaluationError,
    FALLBACK_MODEL,
    FALLBACK_REASON,
    OFFICIAL_DATA_SHA256,
    PUBLISH_LABELS,
    REQUESTED_MODEL,
    canonical_record_hash,
    evaluate_records,
    load_jsonl,
    main,
    render_badcases,
    render_csv,
    score_fault_run,
    score_plan,
    score_run,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
DATA_HASH = "d" * 64


def make_case(case_id: str = "CASE-001") -> dict:
    return {
        "schema_version": "1.0",
        "case_id": case_id,
        "split": "test",
        "category": "synthetic_test",
        "title": "canonical adapter fixture",
        "symptom": "a deterministic measurement is present",
        "context": {},
        "observations": [{"id": "obs-1", "kind": "measurement", "value": 1.0}],
        "interventions": [{"id": "int-1", "before_dbuv": 10.0, "after_dbuv": 9.0}],
        "constraints": [],
        "required_checks": ["query_measurement", "compare_intervention"],
        "tool_data": [
            {
                "source_id": "obs-1",
                "tool": "query_measurement",
                "arguments": {"key": "obs-1"},
                "payload": {
                    "measurement": {"id": "obs-1", "kind": "measurement", "value": 1.0}
                },
                "evidence_tags": ["measurement_present", "auxiliary_measurement"],
            },
            {
                "source_id": "int-1",
                "tool": "compare_intervention",
                "arguments": {"intervention_id": "int-1"},
                "payload": {
                    "intervention_id": "int-1",
                    "before_dbuv": 10.0,
                    "after_dbuv": 9.0,
                    "delta_db": -1.0,
                },
                "evidence_tags": ["intervention_effect"],
            },
        ],
    }


def make_gold(case_id: str = "CASE-001") -> dict:
    return {
        "schema_version": "1.0",
        "case_id": case_id,
        "root_cause_id": "root-a",
        "acceptable_root_cause_ids": ["root-a"],
        "required_evidence_tags": ["measurement_present", "intervention_effect"],
    }


CASE = make_case()
GOLD = make_gold()


def fixture_provenance(execution_mode: str = "fixture") -> dict:
    return {
        "execution_mode": execution_mode,
        "provider": "fixture",
        "model": "fixture-model",
        "config_hash": HASH_A,
        "prompt_hashes": {
            "planner": HASH_B,
            "evidence": HASH_B,
            "diagnosis": HASH_B,
            "reviewer": HASH_B,
        },
        "data_hash": DATA_HASH,
    }


def canonical_run(
    variant: str = "optimized",
    run_kind: str = "evaluation",
    case_id: str = "CASE-001",
) -> dict:
    diagnosis = {
        "candidates": [
            {
                "root_cause_id": "root-a",
                "rank": 1,
                "confidence": 0.82,
                "evidence_ids": ["op-1", "op-2"],
                "label": "fixture root",
                "rationale": "final structured evidence summary",
            }
        ],
        "claims": [
            {
                "claim_id": "claim-1",
                "text": "the measurement supports the candidate",
                "evidence_ids": ["op-1", "op-2"],
                "contradicting_evidence_ids": [],
            }
        ],
    }
    return {
        "schema_version": "2.0",
        "run_id": f"{variant}-{run_kind}-{case_id}",
        "case_id": case_id,
        "variant": variant,
        "run_kind": run_kind,
        "status": "completed",
        "plan": {
            "hypotheses": [
                {"hypothesis_id": "hyp-1", "text": "inspect the measurement"}
            ],
            "steps": [
                {
                    "step_id": "step-1",
                    "hypothesis_id": "hyp-1",
                    "tool": "query_measurement",
                    "arguments": {"key": "obs-1"},
                    "depends_on": [],
                    "completion_condition": "a tagged measurement is returned",
                },
                {
                    "step_id": "step-2",
                    "hypothesis_id": "hyp-1",
                    "tool": "compare_intervention",
                    "arguments": {"intervention_id": "int-1"},
                    "depends_on": [],
                    "completion_condition": "the intervention delta is recorded",
                },
            ],
        },
        "evidence": [
            {
                "evidence_id": "op-1",
                "operation_id": "op-1",
                "step_id": "step-1",
                "tool": "query_measurement",
                "status": "success",
                "phase": "initial",
                "evidence_tags": ["measurement_present"],
                "attempt": 1,
            },
            {
                "evidence_id": "op-2",
                "operation_id": "op-2",
                "step_id": "step-2",
                "tool": "compare_intervention",
                "status": "success",
                "phase": "initial",
                "evidence_tags": ["intervention_effect"],
                "attempt": 1,
            },
        ],
        "diagnosis": diagnosis,
        "review": {"initial_diagnosis": deepcopy(diagnosis), "issues": []},
        "metrics": {
            "model_calls": 4,
            "input_tokens": 100,
            "output_tokens": 40,
            "latency_ms": 250.0,
        },
        "provenance": fixture_provenance(),
        "trajectory": [],
    }


def official_run(case: dict, gold: dict, variant: str, run_kind: str) -> dict:
    run = canonical_run(variant, run_kind, case["case_id"])
    operations: list[str] = []
    steps: list[dict] = []
    evidence: list[dict] = []
    for index, item in enumerate(case["tool_data"], 1):
        operation_id = f"op-{index}"
        step_id = f"step-{index}"
        operations.append(operation_id)
        steps.append(
            {
                "step_id": step_id,
                "hypothesis_id": "hyp-1",
                "tool": item["tool"],
                "arguments": deepcopy(item["arguments"]),
                "depends_on": [],
                "completion_condition": "the deterministic tool payload is recorded",
            }
        )
        evidence.append(
            {
                "evidence_id": operation_id,
                "operation_id": operation_id,
                "step_id": step_id,
                "tool": item["tool"],
                "status": "success",
                "phase": "initial",
                "evidence_tags": deepcopy(item["evidence_tags"]),
                "attempt": 1,
            }
        )
    diagnosis = {
        "candidates": [
            {
                "root_cause_id": gold["root_cause_id"],
                "rank": 1,
                "confidence": 0.8,
                "evidence_ids": operations,
                "label": "synthetic fixture root",
            }
        ],
        "claims": [
            {
                "claim_id": "claim-1",
                "text": "all required deterministic evidence was collected",
                "evidence_ids": operations,
                "contradicting_evidence_ids": [],
            }
        ],
    }
    run["plan"]["steps"] = steps
    run["evidence"] = evidence
    run["diagnosis"] = diagnosis
    run["review"] = {"initial_diagnosis": deepcopy(diagnosis), "issues": []}
    return run


def retry_overlay(
    overlay_id: str = "FAULT-001",
    case_id: str = "CASE-001",
    fault_type: str = "transient_tool_error",
) -> dict:
    trigger = {"attempt": 1, "mode": "raise_once"}
    if fault_type == "timeout_once":
        trigger = {"attempt": 1, "mode": "timeout_once", "timeout_ms": 50}
    return {
        "schema_version": "2.0",
        "overlay_id": overlay_id,
        "case_id": case_id,
        "fault_type": fault_type,
        "target": {
            "node": "evidence_worker",
            "tool": "query_measurement",
            "selector": "first_matching_tool",
        },
        "trigger": trigger,
        "pair_contract": {"baseline": "no_retry", "optimized": "retry_once"},
        "success_conditions": ["trajectory_proof"],
    }


def process_overlay(overlay_id: str, case_id: str) -> dict:
    return {
        "schema_version": "2.0",
        "overlay_id": overlay_id,
        "case_id": case_id,
        "fault_type": "process_interrupt",
        "target": {"node": "diagnosis_agent", "after_node": "evidence_worker"},
        "trigger": {"attempt": 1, "mode": "terminate_after_checkpoint"},
        "pair_contract": {"baseline": "restart", "optimized": "resume"},
        "success_conditions": ["checkpoint_proof"],
    }


def event(event_key: str, node: str, detail: dict) -> dict:
    return {
        "event_key": event_key,
        "node": node,
        "detail": detail,
        "timestamp": "2026-08-20T00:00:00Z",
    }


def add_retry_proof(run: dict, overlay: dict) -> None:
    first_outcome = (
        "failure" if overlay["fault_type"] == "transient_tool_error" else "timeout"
    )
    target_step = next(
        step for step in run["plan"]["steps"] if step["tool"] == overlay["target"]["tool"]
    )
    target_evidence = next(
        item for item in run["evidence"] if item["step_id"] == target_step["step_id"]
    )
    shared = {
        "tool": overlay["target"]["tool"],
        "step_id": target_step["step_id"],
        "operation_id": target_evidence["operation_id"],
    }
    run["trajectory"] = [
        event(
            "event-1",
            "evidence_worker",
            {
                "event_type": "fault_injected",
                "overlay_id": overlay["overlay_id"],
                "fault_type": overlay["fault_type"],
                **shared,
            },
        ),
        event(
            "event-2",
            "evidence_worker",
            {"event_type": "tool_attempt", "tool_attempt": 1, "outcome": first_outcome, **shared},
        ),
        event(
            "event-3",
            "evidence_worker",
            {"event_type": "tool_attempt", "tool_attempt": 2, "outcome": "success", **shared},
        ),
    ]


def add_process_proof(run: dict, overlay: dict) -> None:
    completed_before = overlay["target"]["after_node"]
    run["trajectory"] = [
        event(
            "event-1",
            completed_before,
            {
                "event_type": "node_completed",
                "node": completed_before,
                "outcome": "success",
                "process_instance_id": "process-before",
            },
        ),
        event(
            "event-2",
            overlay["target"]["node"],
            {
                "event_type": "fault_injected",
                "overlay_id": overlay["overlay_id"],
                "fault_type": "process_interrupt",
                "process_instance_id": "process-before",
                "checkpoint_id": "checkpoint-1",
            },
        ),
        event(
            "event-3",
            "graph",
            {
                "event_type": "checkpoint_resume",
                "checkpoint_resume": {
                    "from": {
                        "process_instance_id": "process-before",
                        "checkpoint_id": "checkpoint-1",
                    },
                    "to": {
                        "process_instance_id": "process-after",
                        "resumed_from_checkpoint_id": "checkpoint-1",
                    },
                },
            },
        ),
        event(
            "event-4",
            "reviewer_agent",
            {
                "event_type": "node_completed",
                "node": "reviewer_agent",
                "outcome": "success",
                "process_instance_id": "process-after",
            },
        ),
    ]


class PlanScoringTests(unittest.TestCase):
    def test_valid_plan(self) -> None:
        score = score_plan(canonical_run(), CASE)
        self.assertTrue(score["executable"])
        self.assertEqual(score["invalid_step_count"], 0)

    def test_invalid_argument_dependency_and_redundancy_are_rederived(self) -> None:
        run = canonical_run()
        run["plan"]["steps"] = [
            {
                "step_id": "step-1",
                "hypothesis_id": "hyp-1",
                "tool": "query_measurement",
                "arguments": {},
                "depends_on": ["missing"],
                "completion_condition": "",
            },
            {
                "step_id": "step-2",
                "hypothesis_id": "hyp-1",
                "tool": "query_measurement",
                "arguments": {},
                "depends_on": [],
                "completion_condition": "done",
            },
        ]
        score = score_plan(run, CASE)
        reasons = {reason for item in score["invalid_steps"] for reason in item["reasons"]}
        self.assertFalse(score["executable"])
        self.assertIn("missing_arguments:key", reasons)
        self.assertIn("dependency_missing_or_not_prior", reasons)
        self.assertIn("duplicate_or_redundant_step", reasons)

    def test_blind_plan_does_not_require_hidden_required_check_coverage(self) -> None:
        run = canonical_run()
        run["plan"]["steps"] = [run["plan"]["steps"][0]]
        score = score_plan(run, CASE)
        self.assertTrue(score["executable"])
        self.assertEqual(score["missing_required_checks"], [])


class CanonicalRunTests(unittest.TestCase):
    def test_backend_shaped_snapshot_is_scored(self) -> None:
        score = score_run(canonical_run(), CASE, GOLD)
        self.assertTrue(score["task_completed"])
        self.assertTrue(score["top_candidate_grounded"])
        self.assertTrue(score["top1_hit"])
        self.assertEqual(score["claim_statuses"], {"claim-1": "supported"})

    def test_graph_state_diagnosis_or_arbitrary_extra_is_rejected(self) -> None:
        run = canonical_run()
        run["diagnosis"]["root_causes"] = []
        with self.assertRaises(EvaluationError):
            score_run(run, CASE, GOLD)
        run = canonical_run()
        run["diagnosis"]["candidates"][0]["uncontracted"] = True
        with self.assertRaises(EvaluationError):
            score_run(run, CASE, GOLD)

    def test_old_claim_field_is_rejected_and_contradiction_has_priority(self) -> None:
        run = canonical_run()
        run["diagnosis"]["claims"][0]["contradicted_by"] = ["op-1"]
        with self.assertRaises(EvaluationError):
            score_run(run, CASE, GOLD)
        run = canonical_run()
        run["diagnosis"]["claims"][0]["contradicting_evidence_ids"] = ["op-1"]
        score = score_run(run, CASE, GOLD)
        self.assertEqual(score["claim_statuses"]["claim-1"], "contradicted")

    def test_duplicate_operation_and_claim_ids_are_rejected(self) -> None:
        run = canonical_run()
        run["evidence"].append(deepcopy(run["evidence"][0]))
        with self.assertRaises(EvaluationError):
            score_run(run, CASE, GOLD)
        run = canonical_run()
        run["diagnosis"]["claims"].append(deepcopy(run["diagnosis"]["claims"][0]))
        with self.assertRaises(EvaluationError):
            score_run(run, CASE, GOLD)

    def test_gold_tag_cannot_be_claimed_through_the_wrong_tool(self) -> None:
        run = canonical_run()
        run["evidence"][0]["phase"] = "rework"
        run["evidence"][0]["tool"] = "compare_intervention"
        run["review"] = {
            "initial_diagnosis": {"candidates": [], "claims": []},
            "issues": [],
        }
        with self.assertRaises(EvaluationError):
            score_run(run, CASE, GOLD)

    def test_missing_gold_tag_and_ungrounded_candidate_block_completion(self) -> None:
        run = canonical_run()
        run["evidence"][0]["evidence_tags"] = ["auxiliary_measurement"]
        self.assertFalse(score_run(run, CASE, GOLD)["task_completed"])
        run = canonical_run()
        run["diagnosis"]["candidates"][0]["evidence_ids"] = []
        self.assertFalse(score_run(run, CASE, GOLD)["task_completed"])


class ReviewerTests(unittest.TestCase):
    def _initially_unsupported(self, report_issue: bool) -> dict:
        run = canonical_run()
        initial = deepcopy(run["diagnosis"])
        initial["claims"][0]["evidence_ids"] = []
        issues = []
        if report_issue:
            issues = [
                {
                    "issue_id": "issue-1",
                    "issue_type": "unsupported_claim",
                    "target_id": "claim-1",
                    "resolved": False,
                }
            ]
        run["review"] = {"initial_diagnosis": initial, "issues": issues}
        return run

    def test_valid_issue_and_resolution_are_rederived_from_initial_state(self) -> None:
        score = score_run(self._initially_unsupported(True), CASE, GOLD)
        self.assertEqual(score["review"]["valid_count"], 1)
        self.assertEqual(score["review"]["resolved_count"], 1)
        self.assertTrue(score["task_completed"])

    def test_model_omission_stays_in_denominator_and_is_not_resolved(self) -> None:
        score = score_run(self._initially_unsupported(False), CASE, GOLD)
        self.assertEqual(score["review"]["valid_count"], 1)
        self.assertEqual(score["review"]["reported_valid_count"], 0)
        self.assertEqual(score["review"]["unresolved_valid_count"], 1)
        self.assertFalse(score["task_completed"])


class FaultProofTests(unittest.TestCase):
    def test_retry_requires_one_operation_and_exact_failure_success_attempts(self) -> None:
        overlay = retry_overlay()
        run = canonical_run(run_kind="fault_injection")
        run["fault"] = {"overlay_id": overlay["overlay_id"]}
        add_retry_proof(run, overlay)
        self.assertTrue(score_fault_run(run, overlay, CASE, GOLD)["recovered"])
        run["trajectory"][2]["detail"]["operation_id"] = "different-operation"
        self.assertFalse(score_fault_run(run, overlay, CASE, GOLD)["recovered"])

    def test_process_resume_requires_checkpoint_identity_and_no_reexecution(self) -> None:
        overlay = process_overlay("FAULT-009", "CASE-001")
        run = canonical_run(run_kind="fault_injection")
        run["fault"] = {"overlay_id": overlay["overlay_id"]}
        add_process_proof(run, overlay)
        self.assertTrue(score_fault_run(run, overlay, CASE, GOLD)["recovered"])
        run["trajectory"].append(
            event(
                "event-5",
                "evidence_worker",
                {
                    "event_type": "node_completed",
                    "node": "evidence_worker",
                    "outcome": "success",
                    "process_instance_id": "process-after",
                },
            )
        )
        self.assertFalse(score_fault_run(run, overlay, CASE, GOLD)["recovered"])

    def test_process_resume_binds_fault_to_checkpoint_after_completed_node(self) -> None:
        overlay = process_overlay("FAULT-009", "CASE-001")
        run = canonical_run(run_kind="fault_injection")
        run["fault"] = {"overlay_id": overlay["overlay_id"]}
        add_process_proof(run, overlay)
        run["trajectory"][1]["detail"]["checkpoint_id"] = "unrelated-checkpoint"
        self.assertFalse(score_fault_run(run, overlay, CASE, GOLD)["recovered"])

        add_process_proof(run, overlay)
        run["trajectory"][0], run["trajectory"][1] = (
            run["trajectory"][1],
            run["trajectory"][0],
        )
        self.assertFalse(score_fault_run(run, overlay, CASE, GOLD)["recovered"])


def publish_fixture() -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    data_root = Path(__file__).resolve().parents[1] / "data"
    cases = load_jsonl(data_root / "frozen" / "cases.jsonl")
    gold = load_jsonl(data_root / "frozen" / "gold.jsonl")
    overlays = load_jsonl(data_root / "faults" / "overlays.jsonl")
    gold_by_id = {item["case_id"]: item for item in gold}

    runs: list[dict] = []
    source_ids: dict[tuple[str, str], str] = {}
    for variant in ("baseline", "optimized"):
        config_hash = HASH_A if variant == "baseline" else HASH_C
        for case in cases:
            run = official_run(
                case, gold_by_id[case["case_id"]], variant, "evaluation"
            )
            run["run_id"] = f"live-{variant}-{case['case_id']}"
            run["provenance"] = {
                **fixture_provenance("live"),
                "provider": "dashscope",
                "model": REQUESTED_MODEL,
                "requested_model": REQUESTED_MODEL,
                "actual_model": REQUESTED_MODEL,
                "fallback_reason": "",
                "config_hash": config_hash,
                "data_hash": canonical_record_hash(case),
            }
            source_ids[(variant, case["case_id"])] = run["run_id"]
            runs.append(run)
    for variant in ("baseline", "optimized"):
        for overlay in overlays:
            case = next(item for item in cases if item["case_id"] == overlay["case_id"])
            run = official_run(
                case, gold_by_id[case["case_id"]], variant, "fault_injection"
            )
            run["run_id"] = f"replay-{variant}-{overlay['overlay_id']}"
            run["fault"] = {"overlay_id": overlay["overlay_id"]}
            source_id = source_ids[(variant, overlay["case_id"])]
            source = next(item for item in runs if item["run_id"] == source_id)
            run["provenance"] = {
                **deepcopy(source["provenance"]),
                "execution_mode": "replay",
                "source_run_id": source_id,
            }
            if overlay["fault_type"] == "process_interrupt":
                add_process_proof(run, overlay)
            else:
                add_retry_proof(run, overlay)
            runs.append(run)
    return runs, cases, gold, overlays


class AggregationAndPublishTests(unittest.TestCase):
    def test_development_has_fixed_case_denominators_and_no_live_label(self) -> None:
        report = evaluate_records([canonical_run("baseline")], [CASE], [GOLD], [])
        baseline = report["summary"]["variants"]["baseline"]
        optimized = report["summary"]["variants"]["optimized"]
        self.assertEqual(baseline["task_completion_rate"]["denominator"], 1)
        self.assertEqual(optimized["task_completion_rate"]["numerator"], 0)
        self.assertEqual(baseline["cost"]["latency_ms"]["median"], 250.0)
        self.assertEqual(report["summary"]["labels"], DEVELOPMENT_LABELS)
        self.assertNotIn("LIVE", "/".join(report["summary"]["labels"]))

    def test_full_canonical_48_live_plus_24_replay_bundle_is_publishable(self) -> None:
        runs, cases, gold, overlays = publish_fixture()
        report = evaluate_records(
            runs,
            cases,
            gold,
            overlays,
            mode="publish",
            official_data_hashes=OFFICIAL_DATA_SHA256,
        )
        self.assertTrue(report["summary"]["complete"])
        self.assertEqual(report["summary"]["labels"], PUBLISH_LABELS)
        self.assertEqual(report["summary"]["expected"]["cases"], 24)
        self.assertEqual(report["summary"]["expected"]["fault_runs_per_variant"], 12)
        self.assertNotIn(
            "LIVE_SYNTHETIC_SINGLE_RUN",
            report["summary"]["evidence_labels"]["fault_runs"],
        )
        fault_rows = [
            line for line in render_csv(report).splitlines() if ",fault_injection," in line
        ]
        self.assertTrue(fault_rows)
        self.assertTrue(
            all("LIVE_SYNTHETIC_SINGLE_RUN" not in line for line in fault_rows)
        )

    def test_publish_mode_rejects_incomplete_bundle(self) -> None:
        with self.assertRaises(EvaluationError):
            evaluate_records(
                [],
                [CASE],
                [GOLD],
                [],
                mode="publish",
                official_data_hashes=OFFICIAL_DATA_SHA256,
            )

    def test_publish_rejects_47_live_or_23_replay_records(self) -> None:
        runs, cases, gold, overlays = publish_fixture()
        missing_live = next(
            index for index, run in enumerate(runs) if run["run_kind"] == "evaluation"
        )
        runs.pop(missing_live)
        with self.assertRaises(EvaluationError):
            evaluate_records(
                runs,
                cases,
                gold,
                overlays,
                mode="publish",
                official_data_hashes=OFFICIAL_DATA_SHA256,
            )
        runs, cases, gold, overlays = publish_fixture()
        missing_replay = next(
            index for index, run in enumerate(runs) if run["run_kind"] == "fault_injection"
        )
        runs.pop(missing_replay)
        with self.assertRaises(EvaluationError):
            evaluate_records(
                runs,
                cases,
                gold,
                overlays,
                mode="publish",
                official_data_hashes=OFFICIAL_DATA_SHA256,
            )

    def test_snapshot_unavailable_is_the_only_allowed_fallback(self) -> None:
        runs, cases, gold, overlays = publish_fixture()
        for run in runs:
            run["provenance"]["model"] = FALLBACK_MODEL
            run["provenance"]["actual_model"] = FALLBACK_MODEL
            run["provenance"]["fallback_reason"] = FALLBACK_REASON
        evaluate_records(
            runs,
            cases,
            gold,
            overlays,
            mode="publish",
            official_data_hashes=OFFICIAL_DATA_SHA256,
        )
        runs[0]["provenance"]["fallback_reason"] = "network_error"
        with self.assertRaises(EvaluationError):
            evaluate_records(
                runs,
                cases,
                gold,
                overlays,
                mode="publish",
                official_data_hashes=OFFICIAL_DATA_SHA256,
            )

    def test_publish_rejects_prompt_case_hash_and_fault_source_drift(self) -> None:
        runs, cases, gold, overlays = publish_fixture()
        runs[0]["provenance"]["prompt_hashes"]["reviewer"] = HASH_C
        with self.assertRaises(EvaluationError):
            evaluate_records(
                runs,
                cases,
                gold,
                overlays,
                mode="publish",
                official_data_hashes=OFFICIAL_DATA_SHA256,
            )
        runs, cases, gold, overlays = publish_fixture()
        runs[0]["provenance"]["data_hash"] = HASH_A
        with self.assertRaises(EvaluationError):
            evaluate_records(
                runs,
                cases,
                gold,
                overlays,
                mode="publish",
                official_data_hashes=OFFICIAL_DATA_SHA256,
            )
        runs, cases, gold, overlays = publish_fixture()
        fault_run = next(run for run in runs if run["run_kind"] == "fault_injection")
        wrong_source = next(
            run
            for run in runs
            if run["run_kind"] == "evaluation"
            and run["variant"] == fault_run["variant"]
            and run["case_id"] != fault_run["case_id"]
        )
        fault_run["provenance"]["source_run_id"] = wrong_source["run_id"]
        with self.assertRaises(EvaluationError):
            evaluate_records(
                runs,
                cases,
                gold,
                overlays,
                mode="publish",
                official_data_hashes=OFFICIAL_DATA_SHA256,
            )

    def test_cli_rejection_occurs_before_any_output_write(self) -> None:
        args = SimpleNamespace(
            mode="publish",
            runs=Path("runs.jsonl"),
            cases=Path("cases.jsonl"),
            gold=Path("gold.jsonl"),
            faults=Path("overlays.jsonl"),
            output_dir=Path("never-written"),
        )
        with (
            patch("evaluation.scoring._parse_args", return_value=args),
            patch(
                "evaluation.scoring.load_jsonl",
                side_effect=[[], [CASE], [GOLD], []],
            ),
            patch(
                "evaluation.scoring._official_data_hashes",
                return_value=OFFICIAL_DATA_SHA256,
            ),
            patch("evaluation.scoring.write_reports") as writer,
        ):
            self.assertEqual(main([]), 2)
            writer.assert_not_called()

    def test_csv_and_badcases_keep_mode_labels_and_traceability(self) -> None:
        report = evaluate_records([], [CASE], [GOLD], [])
        csv_text = render_csv(report)
        badcases = render_badcases(report)
        self.assertIn("labels,row_type,variant,case_id", csv_text)
        self.assertIn("missing_run", csv_text)
        for label in DEVELOPMENT_LABELS:
            self.assertIn(label, csv_text)
            self.assertIn(label, badcases)


if __name__ == "__main__":
    unittest.main()
