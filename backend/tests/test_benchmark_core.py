import json
import tempfile
import unittest
from pathlib import Path

from evaluations.benchmark_core import load_dataset, summarize_results
from evaluations.run_benchmark import write_reports


class BenchmarkDatasetTests(unittest.TestCase):
    def test_default_dataset_has_unique_valid_tasks(self):
        dataset = Path(__file__).resolve().parents[1] / "evaluations" / "tasks.jsonl"
        tasks = load_dataset(dataset)
        self.assertEqual(len(tasks), 4)
        self.assertEqual(len({item["id"] for item in tasks}), 4)
        self.assertTrue(all(item["repository"] == "agentharness-benchmark-fixture" for item in tasks))
        self.assertTrue(all(item["test_command"].startswith("pytest -q tests/") for item in tasks))

    def test_scoped_pytest_path_is_accepted_but_shell_injection_is_rejected(self):
        good = {"id": "scoped", "repository": "repo", "input": "A sufficiently detailed test task", "test_command": "pytest -q tests/test_specific.py"}
        bad = {**good, "test_command": "pytest -q tests/test_specific.py && rm -rf /"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.jsonl"
            path.write_text(json.dumps(good), encoding="utf-8")
            self.assertEqual(load_dataset(path)[0]["test_command"], good["test_command"])
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported test_command"):
                load_dataset(path)

    def test_duplicate_task_ids_are_rejected(self):
        row = {"id": "same", "repository": "repo", "input": "A sufficiently detailed test task", "test_command": "auto"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.jsonl"
            path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate task id"):
                load_dataset(path)

    def test_unsafe_or_unknown_test_command_is_rejected(self):
        row = {"id": "bad-command", "repository": "repo", "input": "A sufficiently detailed test task", "test_command": "pytest && rm -rf /"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.jsonl"
            path.write_text(json.dumps(row), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported test_command"):
                load_dataset(path)

    def test_summary_uses_actual_result_rows(self):
        summary = summarize_results([
            {"success": True, "duration_ms": 100, "total_tokens": 1000, "llm_calls": 3, "test_passed": True, "review_approved": True},
            {"success": False, "duration_ms": 300, "total_tokens": 500, "llm_calls": 2, "test_passed": True, "review_approved": False},
        ])
        self.assertEqual(summary["attempted_samples"], 2)
        self.assertEqual(summary["successful_samples"], 1)
        self.assertEqual(summary["success_rate_percent"], 50.0)
        self.assertEqual(summary["total_tokens"], 1500)
        self.assertEqual(summary["total_llm_calls"], 5)
        self.assertEqual(summary["test_pass_count"], 2)
        self.assertEqual(summary["review_approval_count"], 1)

    def test_report_writer_creates_json_csv_and_markdown(self):
        task = {"id": "sample-task", "category": "correctness", "repository": "repo"}
        rows = [{"task_id": "sample-task", "category": "correctness", "repository": "repo", "repetition": 1,
                 "run_id": "run-1", "status": "succeeded", "success": True, "duration_ms": 123,
                 "total_tokens": 99, "prompt_tokens": 60, "completion_tokens": 39, "llm_calls": 2,
                 "test_passed": True, "review_approved": True, "error": None}]
        with tempfile.TemporaryDirectory() as tmp:
            summary = write_reports(Path(tmp), [task], rows, model="test-model")
            self.assertEqual(summary["success_rate_percent"], 100.0)
            for name in ("results.json", "summary.json", "results.csv", "report.md"):
                self.assertTrue((Path(tmp) / name).is_file(), name)
            payload = json.loads((Path(tmp) / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["results"][0]["run_id"], "run-1")

    def test_empty_summary_has_no_fabricated_success_rate(self):
        summary = summarize_results([])
        self.assertEqual(summary["attempted_samples"], 0)
        self.assertIsNone(summary["success_rate_percent"])
        self.assertIsNone(summary["average_duration_ms"])


if __name__ == "__main__":
    unittest.main()
