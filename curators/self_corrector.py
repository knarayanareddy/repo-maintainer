"""Autonomous self-correction engine for curation pipelines.

When preflight, curation, verification, or anti-false-positive evaluation encounters
an error, this module:
1. Dynamically detects and installs any missing packages (Python or Node).
2. Analyzes the failure root cause using Gemini 3.5 Flash.
3. Generates targeted patches for the offending files.
4. Applies the fixes and signals the runner to re-verify.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .auto_installer import auto_install_from_error
from .evaluator import EvaluationVerdict
from .llm import GeminiClient, load_api_key

__all__ = ["SelfCorrectionAttempt", "SelfCorrectionReport", "SelfCorrector"]

MAX_CORRECTION_FILE_SIZE = 50000


class SelfCorrectionAttempt:
    """Record of a single self-correction attempt."""

    def __init__(self, attempt_number: int, failure_type: str, error_message: str) -> None:
        self.attempt_number = attempt_number
        self.failure_type = failure_type  # "dependency" | "verification" | "runtime" | "evaluator"
        self.error_message = error_message
        self.root_cause = ""
        self.files_patched: List[str] = []
        self.packages_installed: List[str] = []
        self.success = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt": self.attempt_number,
            "failure_type": self.failure_type,
            "error_message": self.error_message[:300],
            "root_cause": self.root_cause,
            "files_patched": list(self.files_patched),
            "packages_installed": list(self.packages_installed),
            "success": self.success,
        }


class SelfCorrectionReport:
    """Cumulative history of self-corrections across a repository run."""

    def __init__(self) -> None:
        self.attempts: List[SelfCorrectionAttempt] = []

    @property
    def total_attempts(self) -> int:
        return len(self.attempts)

    @property
    def healed(self) -> bool:
        return bool(self.attempts and self.attempts[-1].success)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_attempts": self.total_attempts,
            "healed": self.healed,
            "attempts": [a.to_dict() for a in self.attempts],
        }


class SelfCorrector:
    """Orchestrates autonomous failure recovery and file patching."""

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        gemini_key: Optional[str] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.gemini_key = gemini_key or load_api_key()
        self._log_fn = log
        self.report = SelfCorrectionReport()

    def log(self, message: str) -> None:
        if self._log_fn:
            self._log_fn(f"[self-correct] {message}")

    def attempt_auto_install(self, error_text: str, cwd: Optional[Path] = None) -> Tuple[bool, str]:
        """Check for and install missing packages from an error trace."""
        self.log("checking for missing dependencies in error trace...")
        installed, details = auto_install_from_error(error_text, cwd=cwd, log_fn=self.log)
        return installed, details

    def correct_verification_problems(
        self,
        repo_name: str,
        workspace_path: Path,
        problems: List[str],
        candidate_files: List[str],
        attempt_idx: int = 1,
    ) -> Tuple[bool, str]:
        """Diagnose verification failures, patch invalid files, and enable retry."""
        attempt = SelfCorrectionAttempt(
            attempt_number=attempt_idx,
            failure_type="verification",
            error_message="; ".join(problems),
        )
        self.report.attempts.append(attempt)

        self.log(f"initiating self-correction (attempt {attempt_idx}/{self.max_attempts})...")

        # 1. First check if any problem indicates a missing package/module
        problem_str = "\n".join(problems)
        installed, install_detail = self.attempt_auto_install(problem_str, cwd=workspace_path)
        if installed:
            attempt.packages_installed.append(install_detail)
            attempt.root_cause = f"Missing dependency resolved: {install_detail}"
            attempt.success = True
            return True, f"Auto-installed missing dependency: {install_detail}"

        if not self.gemini_key:
            return False, "no LLM key available for generative self-correction"

        # 2. Gather file contents for the failing files
        file_contexts: Dict[str, str] = {}
        for rel_path in candidate_files:
            abs_p = workspace_path / rel_path
            if abs_p.is_file() and abs_p.stat().st_size <= MAX_CORRECTION_FILE_SIZE:
                try:
                    file_contexts[rel_path] = abs_p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

        if not file_contexts:
            return False, "no candidate files found to inspect for correction"

        # 3. Query Gemini Flash for targeted corrections
        client = GeminiClient(api_key=self.gemini_key, log=self.log)
        system_prompt = (
            "You are an autonomous self-healing software engineer. A curation job in an open-source "
            "repository failed post-condition validation. "
            "You are given the list of verification problems and the relevant files. "
            "Diagnose the root cause and provide complete, corrected replacements for any file that needs fixing. "
            "Output strictly valid JSON with this structure:\n"
            "{\n"
            '  "root_cause": "concise explanation of why verification failed",\n'
            '  "corrections": [\n'
            '    {\n'
            '      "path": "relative/file/path",\n'
            '      "content": "exact full content of the corrected file"\n'
            '    }\n'
            '  ]\n'
            "}"
        )

        files_snippet = "\n\n".join(
            f"=== FILE: {p} ===\n{content}" for p, content in list(file_contexts.items())[:5]
        )

        user_prompt = (
            f"Repository: {repo_name}\n"
            f"Verification Problems:\n{problem_str}\n\n"
            f"Files:\n{files_snippet}"
        )

        resp = client.complete_json(user_prompt, system=system_prompt, temperature=0.2)
        if not resp or not isinstance(resp, dict):
            return False, "self-correction LLM produced no valid response"

        root_cause = str(resp.get("root_cause") or "Root cause not specified")
        attempt.root_cause = root_cause
        corrections = resp.get("corrections") or []

        if not isinstance(corrections, list) or not corrections:
            return False, f"LLM identified root cause ({root_cause}) but offered no file corrections"

        # 4. Apply corrections
        applied_count = 0
        for item in corrections:
            if not isinstance(item, dict):
                continue
            path_str = str(item.get("path") or "").strip()
            content = item.get("content")
            if not path_str or content is None:
                continue

            target_path = workspace_path / path_str
            # Guardrail: never write outside the workspace
            try:
                target_path.resolve().relative_to(workspace_path.resolve())
            except ValueError:
                self.log(f"rejected unsafe patch path: {path_str}")
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(str(content), encoding="utf-8")
            attempt.files_patched.append(path_str)
            applied_count += 1
            self.log(f"patched file: {path_str}")

        if applied_count > 0:
            attempt.success = True
            return True, f"Successfully self-corrected {applied_count} file(s): {root_cause}"

        return False, "no valid patches could be applied"

    def correct_evaluation_rejection(
        self,
        repo_name: str,
        workspace_path: Path,
        verdict: EvaluationVerdict,
        candidate_files: List[str],
        attempt_idx: int = 1,
    ) -> Tuple[bool, str]:
        """Rectify false-positive or low-quality issues flagged by Jev or Gemini."""
        attempt = SelfCorrectionAttempt(
            attempt_number=attempt_idx,
            failure_type="evaluator",
            error_message=f"Evaluation rejected ({verdict.verdict}, score={verdict.quality_score}): {verdict.reason}",
        )
        self.report.attempts.append(attempt)

        self.log(f"self-correcting evaluation rejection (attempt {attempt_idx}/{self.max_attempts})...")

        if not self.gemini_key:
            return False, "no LLM key available for generative self-correction"

        file_contexts: Dict[str, str] = {}
        for rel_path in candidate_files:
            abs_p = workspace_path / rel_path
            if abs_p.is_file() and abs_p.stat().st_size <= MAX_CORRECTION_FILE_SIZE:
                try:
                    file_contexts[rel_path] = abs_p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

        client = GeminiClient(api_key=self.gemini_key, log=self.log)
        system_prompt = (
            "You are an autonomous expert software author. Your previous output was evaluated by an automated judge "
            "and rejected as a potential false positive or having quality issues (e.g. placeholder stubs, incomplete TODOs, "
            "or insufficient substance). "
            "Your task is to replace any placeholder, stub, or low-quality section with rich, complete, production-ready "
            "implementations. "
            "Output strictly valid JSON with:\n"
            "{\n"
            '  "root_cause": "why the output was shallow or incomplete",\n'
            '  "corrections": [\n'
            '    {\n'
            '      "path": "relative/file/path",\n'
            '      "content": "exact full content of the enriched production-ready file"\n'
            '    }\n'
            '  ]\n'
            "}"
        )

        issues_str = "\n".join(f"- {issue}" for issue in verdict.issues) or verdict.reason
        files_snippet = "\n\n".join(
            f"=== FILE: {p} ===\n{content}" for p, content in list(file_contexts.items())[:5]
        )

        user_prompt = (
            f"Repository: {repo_name}\n"
            f"Evaluator Verdict: {verdict.verdict} (Score: {verdict.quality_score}/10, Model: {verdict.evaluator_model})\n"
            f"Issues Flagged:\n{issues_str}\n\n"
            f"Files to enrich:\n{files_snippet}"
        )

        resp = client.complete_json(user_prompt, system=system_prompt, temperature=0.3)
        if not resp or not isinstance(resp, dict):
            return False, "enrichment LLM produced no valid response"

        root_cause = str(resp.get("root_cause") or "Enriched hollow content")
        attempt.root_cause = root_cause
        corrections = resp.get("corrections") or []

        applied = 0
        for item in corrections:
            if not isinstance(item, dict):
                continue
            path_str = str(item.get("path") or "").strip()
            content = item.get("content")
            if not path_str or content is None:
                continue
            target_path = workspace_path / path_str
            try:
                target_path.resolve().relative_to(workspace_path.resolve())
            except ValueError:
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(str(content), encoding="utf-8")
            attempt.files_patched.append(path_str)
            applied += 1

        if applied > 0:
            attempt.success = True
            return True, f"Enriched {applied} file(s) to eliminate false positives"

        return False, "no files were enriched"
