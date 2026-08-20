# Evaluation badcases

Labels: DEVELOPMENT_V1 / LIVE_SYNTHETIC_SINGLE_RUN / DETERMINISTIC_REPLAY_FAULT_INJECTION / NOT_EXPERT_VALIDATED

Only deterministic failures are listed. This document is not an expert review.

| Type | Variant | Case | Overlay | Failures |
|---|---|---|---|---|
| evaluation | baseline | EVAL-CLK-003 | - | claims_not_fully_supported;review_issue_unresolved;task_incomplete;unsupported_or_contradicted_claim |
| evaluation | baseline | EVAL-GND-001 | - | required_evidence_tags_missing;task_incomplete |
| evaluation | baseline | EVAL-GND-002 | - | plan_not_executable;task_incomplete |
| evaluation | baseline | EVAL-GND-003 | - | plan_not_executable;task_incomplete |
| evaluation | baseline | EVAL-IFL-003 | - | plan_not_executable;task_incomplete |
| evaluation | baseline | EVAL-IFL-004 | - | top1_miss |
| evaluation | baseline | EVAL-NFC-003 | - | plan_not_executable;task_incomplete |
| evaluation | baseline | EVAL-NFC-004 | - | plan_not_executable;task_incomplete |
| evaluation | baseline | EVAL-PWR-001 | - | plan_not_executable;task_incomplete |
| evaluation | baseline | EVAL-SHD-001 | - | required_evidence_tags_missing;review_issue_unresolved;task_incomplete |
| evaluation | baseline | EVAL-SHD-002 | - | required_evidence_tags_missing;review_issue_unresolved;task_incomplete |
| evaluation | baseline | EVAL-SHD-004 | - | required_evidence_tags_missing;review_issue_unresolved;task_incomplete |
| fault_injection | baseline | EVAL-PWR-001 | FAULT-001 | retry_trajectory_not_proven;task_incomplete |
| fault_injection | baseline | EVAL-GND-001 | FAULT-002 | fault_event_missing_or_duplicated;retry_trajectory_not_proven;task_incomplete |
| fault_injection | baseline | EVAL-SHD-001 | FAULT-003 | retry_trajectory_not_proven;task_incomplete |
| fault_injection | baseline | EVAL-CLK-001 | FAULT-004 | fault_event_missing_or_duplicated;retry_trajectory_not_proven |
| fault_injection | baseline | EVAL-IFL-001 | FAULT-005 | retry_trajectory_not_proven;task_incomplete |
| fault_injection | baseline | EVAL-NFC-001 | FAULT-006 | retry_trajectory_not_proven;task_incomplete |
| fault_injection | baseline | EVAL-PWR-003 | FAULT-007 | retry_trajectory_not_proven;task_incomplete |
| fault_injection | baseline | EVAL-GND-003 | FAULT-008 | retry_trajectory_not_proven;task_incomplete |
| fault_injection | baseline | EVAL-SHD-003 | FAULT-009 | cross_instance_checkpoint_resume_not_proven;task_incomplete;top1_miss |
| fault_injection | baseline | EVAL-CLK-003 | FAULT-010 | cross_instance_checkpoint_resume_not_proven;fault_event_missing_or_duplicated;task_incomplete |
| fault_injection | baseline | EVAL-IFL-003 | FAULT-011 | cross_instance_checkpoint_resume_not_proven;task_incomplete;top1_miss |
| fault_injection | baseline | EVAL-NFC-003 | FAULT-012 | cross_instance_checkpoint_resume_not_proven;fault_event_missing_or_duplicated;task_incomplete |
| evaluation | optimized | EVAL-IFL-003 | - | required_evidence_tags_missing;task_incomplete |
| evaluation | optimized | EVAL-IFL-004 | - | top1_miss |
| evaluation | optimized | EVAL-PWR-001 | - | plan_not_executable;task_incomplete |
| evaluation | optimized | EVAL-PWR-004 | - | required_evidence_tags_missing;task_incomplete |
| evaluation | optimized | EVAL-SHD-001 | - | required_evidence_tags_missing;review_issue_unresolved;task_incomplete |
| evaluation | optimized | EVAL-SHD-002 | - | plan_not_executable;required_evidence_tags_missing;review_issue_unresolved;task_incomplete |
| evaluation | optimized | EVAL-SHD-003 | - | top1_miss |
| evaluation | optimized | EVAL-SHD-004 | - | required_evidence_tags_missing;review_issue_unresolved;task_incomplete;top1_miss |
| fault_injection | optimized | EVAL-PWR-001 | FAULT-001 | task_incomplete |
| fault_injection | optimized | EVAL-SHD-001 | FAULT-003 | task_incomplete |
| fault_injection | optimized | EVAL-CLK-001 | FAULT-004 | fault_event_missing_or_duplicated;retry_trajectory_not_proven |
| fault_injection | optimized | EVAL-IFL-001 | FAULT-005 | task_incomplete |
| fault_injection | optimized | EVAL-SHD-003 | FAULT-009 | task_incomplete;top1_miss |
| fault_injection | optimized | EVAL-IFL-003 | FAULT-011 | task_incomplete |
