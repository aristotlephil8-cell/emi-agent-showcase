from __future__ import annotations

import unittest

from evaluation.validate_data import validate_data


class CommittedDataTests(unittest.TestCase):
    def test_frozen_dataset_and_fault_balance(self) -> None:
        result = validate_data()
        self.assertEqual(result["dev_cases"], 6)
        self.assertEqual(result["frozen_cases"], 24)
        self.assertEqual(set(result["categories"].values()), {4})
        self.assertEqual(set(result["root_cause_classes"].values()), {4})
        self.assertEqual(result["fault_types"], {
            "process_interrupt": 4,
            "timeout_once": 4,
            "transient_tool_error": 4,
        })
        self.assertEqual(result["gold_tag_sources_verified"], 60)
        self.assertEqual(result["executable_tool_sources_verified"], 150)
        self.assertEqual(result["runtime_answer_key_leaks"], 0)
        self.assertIn("category", result["model_input_projection_excludes"])
        self.assertIn("tool_data", result["model_input_projection_excludes"])
        self.assertTrue(result["semantic_leakage_warnings"])


if __name__ == "__main__":
    unittest.main()
