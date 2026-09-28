"""Tests for the self-evaluating, anti-false-positive, and self-correcting engine."""

import unittest
from pathlib import Path
import tempfile
import json

from curators.auto_installer import (
    detect_missing_python_packages,
    detect_missing_node_packages,
)
from curators.evaluator import CurationEvaluator, EvaluationVerdict
from curators.self_corrector import SelfCorrectionReport, SelfCorrector


class TestAutoInstaller(unittest.TestCase):
    def test_detect_missing_python_packages(self):
        err = "ModuleNotFoundError: No module named 'yaml'\nImportError: cannot import name dateutil from 'dateutil'"
        pkgs = detect_missing_python_packages(err)
        self.assertIn("pyyaml", pkgs)
        self.assertIn("python-dateutil", pkgs)

    def test_detect_missing_node_packages(self):
        err = "Error: Cannot find module 'ajv'\nError: Cannot find module 'ajv-formats'"
        pkgs = detect_missing_node_packages(err)
        self.assertIn("ajv", pkgs)
        self.assertIn("ajv-formats", pkgs)


class TestEvaluatorHeuristics(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_flag_empty_file_false_positive(self):
        empty_file = self.ws / "empty.json"
        empty_file.write_text("", encoding="utf-8")

        evaluator = CurationEvaluator()
        verdict = evaluator.evaluate(
            repo_name="test/repo",
            workspace_path=self.ws,
            files_touched=["empty.json"],
            items_summary=[],
        )
        self.assertTrue(verdict.is_false_positive)
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.verdict, "NEEDS_CORRECTION")

    def test_flag_heavy_placeholder_false_positive(self):
        stub_file = self.ws / "stub.ts"
        stub_file.write_text(
            "// TODO: implement this\n// TODO: fix this\n// TODO: replace\nconst x = 'REPLACE_ME';",
            encoding="utf-8"
        )
        evaluator = CurationEvaluator()
        verdict = evaluator.evaluate(
            repo_name="test/repo",
            workspace_path=self.ws,
            files_touched=["stub.ts"],
            items_summary=[],
        )
        self.assertTrue(verdict.is_false_positive)
        self.assertFalse(verdict.passed)

    def test_pass_genuine_content(self):
        good_file = self.ws / "tool.json"
        good_file.write_text(
            json.dumps({"name": "AgentKit", "stars": 1500, "description": "High performance toolkit"}),
            encoding="utf-8"
        )
        evaluator = CurationEvaluator(typesafe_key="", gemini_key="", openrouter_key="")
        verdict = evaluator.evaluate(
            repo_name="test/repo",
            workspace_path=self.ws,
            files_touched=["tool.json"],
            items_summary=[{"title": "AgentKit"}],
        )
        self.assertFalse(verdict.is_false_positive)
        self.assertTrue(verdict.quality_score >= 6)



class TestSelfCorrector(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_corrector_tracks_attempts(self):
        corrector = SelfCorrector(max_attempts=3)
        self.assertEqual(corrector.max_attempts, 3)
        self.assertEqual(corrector.report.total_attempts, 0)
        self.assertFalse(corrector.report.healed)


if __name__ == "__main__":
    unittest.main()
