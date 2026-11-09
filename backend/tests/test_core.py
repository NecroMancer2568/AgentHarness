import json
import tempfile
import unittest
from pathlib import Path
from app.runner import parse_json_loose
from app.repository import ALLOWED_TEST_COMMANDS, WorkspaceError, ToolRuntime, is_allowed_test_command, resolve_test_command
from app.config import Settings

class ParsePlanTests(unittest.TestCase):
    def test_parse_valid_json(self):
        plan = parse_json_loose('{"summary":"fix bug","steps":["inspect"],"include_security_review":false}')
        self.assertEqual(plan["summary"], "fix bug")
        self.assertFalse(plan["include_security_review"])

    def test_parse_json_inside_fence_or_prose(self):
        plan = parse_json_loose('Plan follows: {"summary":"review","steps":[],"include_security_review":true}')
        self.assertEqual(plan["summary"], "review")
        self.assertTrue(plan["include_security_review"])

    def test_invalid_json_uses_conservative_defaults(self):
        plan = parse_json_loose('not json')
        self.assertIn("risks", plan)
        self.assertFalse(plan["include_security_review"])

class ToolSafetyTests(unittest.TestCase):
    def test_test_command_allowlist_is_explicit(self):
        self.assertIn("pytest", ALLOWED_TEST_COMMANDS)
        self.assertIn("cargo test", ALLOWED_TEST_COMMANDS)
        self.assertNotIn("rm -rf /", ALLOWED_TEST_COMMANDS)

    def test_scoped_pytest_commands_are_safe_and_resolved(self):
        self.assertTrue(is_allowed_test_command("pytest -q tests/test_even.py"))
        self.assertFalse(is_allowed_test_command("pytest -q ../../outside.py"))
        self.assertFalse(is_allowed_test_command("pytest -q tests/test.py && rm -rf /"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_even.py").write_text("def test_ok(): assert True")
            self.assertEqual(resolve_test_command("pytest -q tests/test_even.py", root), ["python", "-m", "pytest", "-q", "tests/test_even.py"])
            self.assertIsNone(resolve_test_command("pytest -q tests/missing.py", root))

    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            settings = Settings(agentharness_max_tool_output_chars=1000)
            runtime = ToolRuntime(root, settings, "auto")
            with self.assertRaises(WorkspaceError):
                runtime._safe_path("../../outside")

if __name__ == '__main__':
    unittest.main()
