"""Self-evaluating and anti-false-positive evaluation layer with Jev support.

This module acts as a strict "System One" decision and evaluation gatekeeper.
Even after a curation run passes verification and reports success, this layer
inspects the work to ensure it is substantive, functional, and NOT a false positive
(e.g., placeholder text, empty stubs, unresolved TODOs, or fake mocks).

It integrates TypeSafe AI's Jev decision model (via OpenRouter or native TypeSafe API)
for ultra-fast, deterministic typed evaluation, with seamless cascade fallback to
Gemini 3.5 Flash and local deterministic heuristics.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .llm import GeminiClient, load_api_key

__all__ = ["EvaluationVerdict", "CurationEvaluator", "evaluate_curation"]

#: Suspicious placeholder patterns that indicate false positives / stubbing.
FALSE_POSITIVE_PATTERNS = [
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
    re.compile(r"\bLOREM\s+IPSUM\b", re.IGNORECASE),
    re.compile(r"\bREPLACE_ME\b", re.IGNORECASE),
    re.compile(r"\bFILL_IN_HERE\b", re.IGNORECASE),
    re.compile(r"\bINSERT_CODE_HERE\b", re.IGNORECASE),
    re.compile(r"\bYOUR_API_KEY\b", re.IGNORECASE),
    re.compile(r"\bEXAMPLE\.COM\b", re.IGNORECASE),
]

#: Max diff length handed to the evaluator to stay within fast context windows.
MAX_DIFF_CHARS = 12000


@dataclass
class EvaluationVerdict:
    """The structured evaluation outcome of a curation run."""

    verdict: str  # "APPROVED" | "NEEDS_CORRECTION" | "REJECTED"
    is_false_positive: bool
    quality_score: int  # 1 to 10
    reason: str
    issues: List[str] = field(default_factory=list)
    evaluator_model: str = "unknown"

    @property
    def passed(self) -> bool:
        """Whether the run is genuinely approved to ship."""
        return self.verdict == "APPROVED" and not self.is_false_positive and self.quality_score >= 6

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "is_false_positive": self.is_false_positive,
            "quality_score": self.quality_score,
            "reason": self.reason,
            "issues": list(self.issues),
            "evaluator_model": self.evaluator_model,
        }


class CurationEvaluator:
    """Evaluates curation results using Jev, Gemini Flash, and heuristics."""

    def __init__(
        self,
        *,
        openrouter_key: Optional[str] = None,
        typesafe_key: Optional[str] = None,
        gemini_key: Optional[str] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.openrouter_key = openrouter_key or os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.typesafe_key = typesafe_key or os.environ.get("TYPESAFE_API_KEY", "").strip()
        self.gemini_key = gemini_key or load_api_key()
        self._log_fn = log

        # Try loading openrouter key from ~/.hermes/idea-dump/keys.env if missing
        if not self.openrouter_key:
            self._load_fallback_keys()

    def _load_fallback_keys(self) -> None:
        key_file = Path("~/.hermes/idea-dump/keys.env").expanduser()
        if key_file.is_file():
            try:
                for line in key_file.read_text(encoding="utf-8").splitlines():
                    if line.startswith("OPENROUTER_API_KEY="):
                        self.openrouter_key = line.split("=", 1)[1].strip().strip("\"'")
                    elif line.startswith("TYPESAFE_API_KEY="):
                        self.typesafe_key = line.split("=", 1)[1].strip().strip("\"'")
            except OSError:
                pass

    def log(self, message: str) -> None:
        if self._log_fn:
            self._log_fn(f"[evaluator] {message}")

    # -- Fast Heuristics ---------------------------------------------------- #

    def _heuristic_check(self, workspace_path: Path, files_touched: List[str]) -> Tuple[List[str], bool]:
        """Rapid local check for 0-byte files, placeholder text, and broken JSON."""
        issues: List[str] = []
        is_false_pos = False

        checked_files = list(files_touched)
        if not checked_files:
            # Fallback to checking workspace via git status
            try:
                res = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(workspace_path),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    for line in res.stdout.decode("utf-8", "replace").splitlines():
                        entry = line[3:].strip()
                        if " -> " in entry:
                            entry = entry.split(" -> ")[-1]
                        if entry:
                            checked_files.append(entry.strip('"'))
            except Exception:
                pass

        if not checked_files:
            return ["No files were modified or created"], True

        for rel_path in checked_files[:20]:
            abs_path = workspace_path / rel_path
            if not abs_path.exists():
                continue

            size = abs_path.stat().st_size
            if size == 0:
                issues.append(f"File {rel_path} is completely empty (0 bytes)")
                is_false_pos = True
                continue

            # For large files (> 2 MB, e.g. repos.json catalog), do not load entirely into memory
            if size > 2 * 1024 * 1024:
                # Fast sample verification
                try:
                    with abs_path.open("rb") as f:
                        header = f.read(1024).strip()
                    if rel_path.endswith(".json") and not (header.startswith(b"[") or header.startswith(b"{")):
                        issues.append(f"Large JSON file {rel_path} does not start with valid JSON root object or array")
                        is_false_pos = True
                except Exception as exc:
                    issues.append(f"Error sampling large file {rel_path}: {exc}")
                continue

            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                issues.append(f"File {rel_path} unreadable: {exc}")
                continue

            # Check JSON files for parsing
            if rel_path.endswith(".json"):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, (list, dict)) and len(parsed) == 0:
                        issues.append(f"JSON file {rel_path} contains an empty collection")
                except json.JSONDecodeError as exc:
                    issues.append(f"JSON file {rel_path} has syntax error: {exc}")
                    is_false_pos = True

            # Check for suspicious mock / placeholder tokens
            suspicious_count = 0
            for pat in FALSE_POSITIVE_PATTERNS:
                matches = pat.findall(content)
                if matches:
                    suspicious_count += len(matches)
            if suspicious_count >= 5:
                issues.append(f"File {rel_path} contains multiple suspicious placeholder tokens ({suspicious_count} found)")
                is_false_pos = True

        return issues, is_false_pos

    # -- Jev Evaluation (Native TypeSafe & OpenRouter) ---------------------- #

    def _evaluate_with_typesafe_native(self, payload_summary: str) -> Optional[EvaluationVerdict]:
        """Query TypeSafe Jev native System One endpoint (api.typesafe.ai/v1/systemone)."""
        key = self.typesafe_key
        if not key:
            return None

        self.log("invoking TypeSafe Jev native System One decision model...")
        endpoint = "https://api.typesafe.ai/v1/systemone"

        body = {
            "model": "jev-latest",
            "state": payload_summary[:8000],
            "questions": {
                "is_false_positive": {
                    "type": "noul",
                    "instructions": "Does this work contain hollow stubs, 0-byte files, or superficial mock placeholders that give a false impression of success?",
                    "criteria": {
                        "true": "Contains hollow mock stubs, unfinished TODOs, or empty files",
                        "false": "Substantive, functional, real code or data"
                    }
                },
                "verdict": {
                    "type": "choice",
                    "instructions": "What is the release decision for this curation pull request?",
                    "criteria": {
                        "APPROVED": "High quality, genuine code ready for merge",
                        "NEEDS_CORRECTION": "Has issues, missing fields, or stub sections that need self-correction",
                        "REJECTED": "Completely invalid or broken"
                    }
                },
                "quality_score": {
                    "type": "score",
                    "instructions": "Rate the overall quality and completeness of this work on a scale from 1 to 5.",
                    "criteria": [
                        "1 - Broken or zero-byte stubs",
                        "2 - Shallow placeholder content",
                        "3 - Basic minimal implementation",
                        "4 - High quality substantive content",
                        "5 - Exceptional production-grade work"
                    ]
                }
            }
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "repo-maintainer-jev/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            answers = data.get("answers", {})
            fp_answer = answers.get("is_false_positive", {})
            verdict_answer = answers.get("verdict", {})
            score_answer = answers.get("quality_score", {})

            is_fp = float(fp_answer.get("noul", 0.0)) >= 0.5
            verdict_str = str(verdict_answer.get("choice", "NEEDS_CORRECTION")).upper()
            confidence = float(verdict_answer.get("confidence", 0.0))
            raw_score = float(score_answer.get("score", 2.5))
            scaled_score = max(1, min(10, round((raw_score + 1) * 2)))
            model_id = str(data.get("model", "jev-latest"))

            issues: List[str] = []
            if is_fp:
                issues.append(f"Jev flagged output as likely false-positive (prob={fp_answer.get('noul', 0.0):.2f})")
            if scaled_score < 6:
                issues.append(f"Jev assessed quality score as {scaled_score}/10 (below 6/10 floor)")

            reason_str = (
                f"Jev decision: {verdict_str} (confidence: {confidence:.2f}, "
                f"false-positive prob: {fp_answer.get('noul', 0.0):.2f}, "
                f"quality: {scaled_score}/10)"
            )
            self.log(f"Jev result: {verdict_str}, score={scaled_score}/10, model={model_id}")

            return EvaluationVerdict(
                verdict=verdict_str,
                is_false_positive=is_fp,
                quality_score=scaled_score,
                reason=reason_str,
                issues=issues,
                evaluator_model=f"typesafe/{model_id}",
            )
        except urllib.error.HTTPError as exc:
            self.log(f"TypeSafe Jev HTTP {exc.code} ({exc.reason}); cascading to OpenRouter/Gemini")
            return None
        except Exception as exc:
            self.log(f"TypeSafe Jev call failed: {exc}; cascading to OpenRouter/Gemini")
            return None

    def _evaluate_with_openrouter_jev(self, payload_summary: str) -> Optional[EvaluationVerdict]:
        """Query TypeSafe Jev model via OpenRouter."""
        key = self.openrouter_key
        if not key:
            return None

        self.log("invoking TypeSafe Jev via OpenRouter...")
        endpoint = "https://openrouter.ai/api/v1/chat/completions"
        model = "typesafe/jev-router"

        system_prompt = (
            "You are Jev, a deterministic System One decision model. "
            "You evaluate whether automated pull requests and file modifications are genuine, "
            "substantive, production-quality work or false positives (stubs, mock placeholders, "
            "or deceptive superficial changes). "
            "Respond strictly in JSON with fields:\n"
            "- is_false_positive: boolean\n"
            "- quality_score: integer from 1 to 10 (>= 6 is pass)\n"
            "- verdict: 'APPROVED' | 'NEEDS_CORRECTION' | 'REJECTED'\n"
            "- reason: concise string explanation\n"
            "- issues: array of strings naming specific deficiencies if any"
        )

        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_summary},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "repo-maintainer-jev/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = json.loads(choice)
            verdict_str = str(parsed.get("verdict", "NEEDS_CORRECTION")).upper()
            if verdict_str not in ("APPROVED", "NEEDS_CORRECTION", "REJECTED"):
                verdict_str = "APPROVED" if parsed.get("quality_score", 0) >= 6 else "NEEDS_CORRECTION"

            return EvaluationVerdict(
                verdict=verdict_str,
                is_false_positive=bool(parsed.get("is_false_positive", False)),
                quality_score=int(parsed.get("quality_score", 5)),
                reason=str(parsed.get("reason", "Evaluated by Jev via OpenRouter")),
                issues=list(parsed.get("issues", [])),
                evaluator_model="openrouter/typesafe-jev",
            )
        except urllib.error.HTTPError as exc:
            self.log(f"Jev OpenRouter HTTP {exc.code} ({exc.reason}); cascading to Gemini Flash")
            return None
        except Exception as exc:
            self.log(f"Jev OpenRouter call failed: {exc}; cascading to Gemini Flash")
            return None

    # -- Gemini Flash Evaluation Cascade ------------------------------------ #

    def _evaluate_with_gemini(self, payload_summary: str) -> Optional[EvaluationVerdict]:
        """Structured decision evaluation using Gemini 3.5 Flash."""
        if not self.gemini_key:
            return None

        self.log("evaluating with Gemini 3.5 Flash structured judge...")
        client = GeminiClient(api_key=self.gemini_key, log=self.log)
        system_prompt = (
            "You are an expert automated QA and anti-false-positive evaluation judge. "
            "You review code contributions generated by autonomous maintenance agents. "
            "Verify that the work is substantive, functional, syntactically sound, and contains "
            "NO hollow mock stubs, unfinished TODOs, or empty files. "
            "Return JSON matching:\n"
            "{\n"
            '  "is_false_positive": boolean,\n'
            '  "quality_score": integer (1-10),\n'
            '  "verdict": "APPROVED" | "NEEDS_CORRECTION" | "REJECTED",\n'
            '  "reason": "summary string",\n'
            '  "issues": ["list", "of", "issues"]\n'
            "}"
        )

        res = client.complete_json(
            payload_summary,
            system=system_prompt,
            temperature=0.2,
        )

        if not res or not isinstance(res, dict):
            return None

        verdict_str = str(res.get("verdict", "NEEDS_CORRECTION")).upper()
        if verdict_str not in ("APPROVED", "NEEDS_CORRECTION", "REJECTED"):
            verdict_str = "APPROVED" if res.get("quality_score", 0) >= 6 else "NEEDS_CORRECTION"

        return EvaluationVerdict(
            verdict=verdict_str,
            is_false_positive=bool(res.get("is_false_positive", False)),
            quality_score=int(res.get("quality_score", 5)),
            reason=str(res.get("reason", "Evaluated by Gemini 3.5 Flash")),
            issues=list(res.get("issues", [])),
            evaluator_model="gemini-3.5-flash",
        )

    # -- Unified Evaluation Entry Point ------------------------------------- #

    def evaluate(
        self,
        repo_name: str,
        workspace_path: Path,
        files_touched: List[str],
        items_summary: List[Dict[str, Any]],
        diff_text: str = "",
    ) -> EvaluationVerdict:
        """Run complete tiered evaluation: Heuristics -> Jev -> Gemini Flash."""
        # 1. Fast heuristics
        heuristic_issues, is_false_pos = self._heuristic_check(workspace_path, files_touched)
        if is_false_pos:
            return EvaluationVerdict(
                verdict="NEEDS_CORRECTION",
                is_false_positive=True,
                quality_score=2,
                reason="Failed local anti-false-positive heuristic checks",
                issues=heuristic_issues,
                evaluator_model="deterministic-heuristic",
            )

        # 2. Build concise diff / summary payload
        sample_diff = diff_text[:MAX_DIFF_CHARS] if diff_text else ""
        if not sample_diff:
            # Construct a preview from modified files
            previews = []
            for path in files_touched[:5]:
                p = workspace_path / path
                if p.is_file():
                    previews.append(f"--- File: {path} ---\n{p.read_text(encoding='utf-8', errors='replace')[:1500]}")
            sample_diff = "\n\n".join(previews)

        payload_summary = (
            f"Repository: {repo_name}\n"
            f"Files Touched ({len(files_touched)}): {', '.join(files_touched[:15])}\n"
            f"Items Created ({len(items_summary)}): {json.dumps(items_summary[:5], indent=1)}\n\n"
            f"Content Sample:\n{sample_diff[:8000]}"
        )

        # 3. Try Native TypeSafe Jev System One
        native_jev = self._evaluate_with_typesafe_native(payload_summary)
        if native_jev is not None:
            if heuristic_issues:
                native_jev.issues.extend(heuristic_issues)
            return native_jev

        # 3b. Try OpenRouter Jev
        router_jev = self._evaluate_with_openrouter_jev(payload_summary)
        if router_jev is not None:
            if heuristic_issues:
                router_jev.issues.extend(heuristic_issues)
            return router_jev

        # 4. Cascade to Gemini Flash
        gemini_verdict = self._evaluate_with_gemini(payload_summary)
        if gemini_verdict is not None:
            if heuristic_issues:
                gemini_verdict.issues.extend(heuristic_issues)
            return gemini_verdict

        # 5. Deterministic fallback if all LLMs are offline
        score = 8 if not heuristic_issues else 5
        verdict_str = "APPROVED" if score >= 6 else "NEEDS_CORRECTION"
        return EvaluationVerdict(
            verdict=verdict_str,
            is_false_positive=False,
            quality_score=score,
            reason="Heuristic evaluation passed (LLMs offline)",
            issues=heuristic_issues,
            evaluator_model="deterministic-fallback",
        )


def evaluate_curation(
    repo_name: str,
    workspace_path: Path,
    files_touched: List[str],
    items_summary: List[Dict[str, Any]],
    diff_text: str = "",
    log_fn: Optional[Callable[[str], None]] = None,
) -> EvaluationVerdict:
    """Convenience helper to evaluate a completed curation run."""
    evaluator = CurationEvaluator(log=log_fn)
    return evaluator.evaluate(
        repo_name=repo_name,
        workspace_path=workspace_path,
        files_touched=files_touched,
        items_summary=items_summary,
        diff_text=diff_text,
    )
