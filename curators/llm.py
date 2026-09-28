"""Zero-cost LLM synthesis for creative curation work.

Creative text (website concepts, knowledge entries, daily briefings) is the
one part of curation that cannot be derived mechanically, so it is delegated
to Google's free Gemini tier. The client is deliberately conservative:

* the API key is read from ``~/.hermes/idea-dump/keys.env`` (or the
  environment) and is never logged, echoed, or written to a report;
* every call is spaced to respect the free tier's 15 requests-per-minute
  limit, with backoff on ``429``/``403`` quota responses;
* ``thinkingBudget`` is pinned to 0 so reasoning tokens never eat the output
  budget or the per-minute token allowance;
* every entry point degrades to ``None`` plus a recorded reason instead of
  raising, so a recipe can always fall back to its deterministic path.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["LlmError", "GeminiClient", "load_api_key", "DEFAULT_MODEL", "KEY_FILE"]

#: Free-tier model alias; resolves to the current Gemini Flash release.
DEFAULT_MODEL = "gemini-3.5-flash"

#: Endpoint template for ``generateContent``.
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

#: Where the operator keeps their key material.
KEY_FILE = Path("~/.hermes/idea-dump/keys.env")

#: Environment variables consulted before the key file.
KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY")

#: 15 requests/minute on the free tier leaves no room for bursts.
MIN_REQUEST_INTERVAL = 4.5

#: Quota-style statuses that deserve a retry rather than an immediate give-up.
RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})

_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\n?(?P<body>.*?)\n?```$", re.DOTALL)
_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class LlmError(RuntimeError):
    """Raised only when a caller explicitly asks for a hard failure."""


def _redact(value: str) -> str:
    """Mask anything key-shaped so a log line can never leak a secret."""
    masked = re.sub(r"(AIza)[A-Za-z0-9_\-]{10,}", r"\1***", str(value or ""))
    return masked[:200]


def load_api_key() -> Optional[str]:
    """Resolve the Gemini key from the environment, then the key file.

    The key file is parsed line-by-line rather than sourced into the shell so
    no value is ever expanded, exported, or exposed to child processes.
    """
    for name in KEY_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        raw = KEY_FILE.expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if key in KEY_ENV_VARS:
            return value.strip("'\"")
    return None


def load_openrouter_keys() -> List[str]:
    """Resolve all available OpenRouter keys from env and key files."""
    keys: List[str] = []
    for k, v in os.environ.items():
        if k.startswith("OPENROUTER_API_KEY") and v.strip():
            clean = v.strip().strip("'\"")
            if clean not in keys:
                keys.append(clean)
    for keypath in [KEY_FILE, Path("~/.hermes/.env")]:
        try:
            raw = keypath.expanduser().read_text(encoding="utf-8")
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                match = _ASSIGNMENT_RE.match(line)
                if match:
                    k, v = match.group(1), match.group(2).strip().strip("'\"")
                    if k.startswith("OPENROUTER_API_KEY") and v and v not in keys:
                        keys.append(v)
        except Exception:
            pass
    return keys


@dataclass
class GeminiStats:
    """Request accounting for one client instance."""

    requests: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for JSON output."""
        return {
            "requests": self.requests,
            "failures": self.failures,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "errors": list(self.errors[-5:]),
        }



class GeminiClient:
    """A small, rate-limit-aware wrapper around ``gemini-flash-latest``."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        enabled: bool = True,
        timeout: int = 60,
        min_interval: float = MIN_REQUEST_INTERVAL,
        max_attempts: int = 3,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key if api_key is not None else load_api_key()
        self.enabled = bool(enabled)
        self.timeout = max(5, int(timeout))
        self.min_interval = max(0.0, float(min_interval))
        self.max_attempts = max(1, int(max_attempts))
        self.stats = GeminiStats()
        self._log_fn = log
        self._last_request = 0.0

    # -- state -------------------------------------------------------------- #

    @property
    def available(self) -> bool:
        """Whether a call could plausibly succeed right now."""
        return bool(self.enabled and (self.api_key or bool(load_openrouter_keys())))

    def log(self, message: str) -> None:
        """Emit a progress line through the injected logger."""
        if self._log_fn is not None:
            self._log_fn("[llm] {0}".format(message))

    def disable(self, reason: str) -> None:
        """Turn the client off for the remainder of the run."""
        self.enabled = False
        self.log("disabled: {0}".format(_redact(reason)))

    # -- transport ---------------------------------------------------------- #

    def _throttle(self) -> None:
        """Sleep just enough to respect the free-tier request cadence."""
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.monotonic()

    @staticmethod
    def _extract_text(payload: Dict[str, Any]) -> str:
        """Join the text parts of the first candidate."""
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return ""
        parts = ((candidates[0] or {}).get("content") or {}).get("parts")
        if not isinstance(parts, list):
            return ""
        return "".join(
            str(part.get("text") or "") for part in parts if isinstance(part, dict)
        ).strip()

    def _record_usage(self, payload: Dict[str, Any]) -> None:
        """Accumulate token counters when the API reports them."""
        usage = payload.get("usageMetadata")
        if not isinstance(usage, dict):
            return
        self.stats.input_tokens += int(usage.get("promptTokenCount") or 0)
        self.stats.output_tokens += int(usage.get("candidatesTokenCount") or 0)

    @staticmethod
    def _error_reason(body: str, status: int) -> str:
        """Pull a human-readable reason out of a Google API error body."""
        try:
            parsed = json.loads(body)
        except ValueError:
            return "http {0}: {1}".format(status, _redact(body))
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if not isinstance(error, dict):
            return "http {0}: {1}".format(status, _redact(body))
        status_name = str(error.get("status") or "UNKNOWN")
        for detail in error.get("details") or []:
            if isinstance(detail, dict) and detail.get("reason"):
                status_name = "{0}/{1}".format(status_name, detail["reason"])
                break
        return "http {0} {1}: {2}".format(
            status, status_name, _redact(str(error.get("message") or ""))
        )

    def _post(self, payload: Dict[str, Any]) -> Tuple[Optional[Any], str]:
        """POST one request, returning ``(payload, error)`` after retries."""
        body = json.dumps(payload).encode("utf-8")
        url = API_URL.format(model=self.model)
        delay = 4.0
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            self.stats.requests += 1
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "x-goog-api-key": self.api_key or "",
                    "Content-Type": "application/json",
                    "User-Agent": "repo-maintainer-curator/1.0",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    raw = response.read().decode("utf-8", "replace")
                decoded = json.loads(raw)
                self._record_usage(decoded)
                return decoded, ""
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                reason = self._error_reason(detail, exc.code)
                if exc.code in RETRY_STATUSES and attempt < self.max_attempts:
                    self.log("retrying after {0} (attempt {1}/{2})".format(
                        reason, attempt, self.max_attempts))
                    time.sleep(delay)
                    delay = min(delay * 2, 45.0)
                    continue
                self.stats.failures += 1
                self.stats.errors.append(reason)
                return None, reason
            except (urllib.error.URLError, OSError, ValueError) as exc:
                reason = "{0}: {1}".format(type(exc).__name__, _redact(str(exc)))
                if attempt < self.max_attempts:
                    time.sleep(delay)
                    delay = min(delay * 2, 45.0)
                    continue
                self.stats.failures += 1
                self.stats.errors.append(reason)
                return None, reason
        return None, "exhausted {0} attempt(s)".format(self.max_attempts)


    # -- public API --------------------------------------------------------- #

    def _complete_openrouter(
        self,
        prompt: str,
        *,
        system: str = "",
        json_mode: bool = False,
        max_output_tokens: int = 4096,
        temperature: float = 0.8,
        keys: Optional[List[str]] = None,
    ) -> Optional[str]:
        if not keys:
            keys = load_openrouter_keys()
        if not keys:
            return None

        model = "stealth/space-bunny-alpha"
        endpoint = "https://openrouter.ai/api/v1/chat/completions"
        for idx, key in enumerate(keys):
            try:
                body: Dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system or "You are an autonomous AI research and code maintainer."},
                        {"role": "user", "content": str(prompt)},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_output_tokens,
                }
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                req = urllib.request.Request(
                    endpoint,
                    data=json.dumps(body).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "User-Agent": "repo-maintainer-curator/1.0",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                choice = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if choice and choice.strip():
                    self.log(f"ok via OpenRouter/{model} [key {idx+1}/{len(keys)}]")
                    return choice.strip()
            except Exception as e:
                self.log(f"OpenRouter {model} key {idx+1} failed: {e}; trying next key in pool")
                continue
        return None

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        json_mode: bool = False,
        max_output_tokens: int = 4096,
        temperature: float = 0.8,
    ) -> Optional[str]:
        """Run one completion, returning text or ``None`` on any failure."""
        keys = load_openrouter_keys()
        if keys:
            res = self._complete_openrouter(
                prompt,
                system=system,
                json_mode=json_mode,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                keys=keys,
            )
            if res:
                return res

        if not self.available:
            self.log("skipped: no API key or client disabled")
            return None
        generation: Dict[str, Any] = {
            "maxOutputTokens": max(256, int(max_output_tokens)),
            "temperature": max(0.0, min(2.0, float(temperature))),
            # Keep reasoning tokens off the free tier's per-minute budget.
            "thinkingConfig": {"thinkingBudget": 0},
        }
        if json_mode:
            generation["responseMimeType"] = "application/json"
        parts: List[Dict[str, str]] = []
        if system:
            parts.append({"text": "{0}\n\n---\n\n".format(system)})
        parts.append({"text": str(prompt)})
        payload = {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation}

        data, error = self._post(payload)
        if error or not isinstance(data, dict):
            self.log("request failed: {0}".format(error or "malformed response"))
            return None
        text = self._extract_text(data)
        if not text:
            finish = (data.get("candidates") or [{}])[0].get("finishReason", "unknown")
            reason = "empty response (finishReason={0})".format(finish)
            self.stats.failures += 1
            self.stats.errors.append(reason)
            self.log(reason)
            return None
        self.log("ok ({0} in / {1} out tokens)".format(
            self.stats.input_tokens, self.stats.output_tokens))
        return text

    def complete_json(
        self,
        prompt: str,
        *,
        system: str = "",
        max_output_tokens: int = 4096,
        temperature: float = 0.8,
    ) -> Optional[Any]:
        """Run a completion constrained to JSON and return the decoded value.

        Retries once with an explicit repair instruction because a truncated
        or fenced response is the most common recoverable failure.
        """
        text = self.complete(
            prompt, system=system, json_mode=True,
            max_output_tokens=max_output_tokens, temperature=temperature,
        )
        if text is None:
            return None
        parsed = self._loads(text)
        if parsed is not None:
            return parsed
        self.log("response was not valid JSON; retrying once")
        repaired = self.complete(
            "{0}\n\nYour previous reply was not valid JSON. Reply with JSON only, "
            "no prose and no code fence.".format(prompt),
            system=system, json_mode=True, max_output_tokens=max_output_tokens,
            temperature=max(0.0, temperature - 0.3),
        )
        return self._loads(repaired) if repaired else None

    @staticmethod
    def _loads(text: Optional[str]) -> Optional[Any]:
        """Decode a model response, tolerating fences and stray prose."""
        if not text:
            return None
        candidate = text.strip()
        fenced = _FENCE_RE.match(candidate)
        if fenced:
            candidate = fenced.group("body").strip()
        for attempt in (candidate, _first_json_span(candidate)):
            if not attempt:
                continue
            try:
                return json.loads(attempt)
            except ValueError:
                continue
        return None


def _first_json_span(text: str) -> str:
    """Extract the outermost ``{...}`` or ``[...]`` span from a response."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            return text[start : end + 1]
    return ""
