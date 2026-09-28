#!/usr/bin/env python3
"""Autonomous multi-phase maintenance engine for GitHub repositories.

``repo_maintainer.py`` is the core CLI driver of the repo-maintainer system.
It operates on the repositories declared in ``config/repos.json`` and runs
through four distinct operational phases.

Phase 1 -- Observer
    Read-only health and staleness audit. Each target repository is cloned or
    fetched into an isolated ``workspace/`` tree and inspected for commit
    activity, documentation hygiene, dependency freshness, and test/CI
    coverage. A structured Markdown report is written to
    ``reports/health_report_<repo>_<date>.md`` and **zero** changes are made
    to the target repository.

Phase 2 -- Automated PR
    Non-destructive upkeep on a dedicated branch
    (``chore/daily-maintenance-<date>``): formatting, lint autofix, safe
    patch dependency bumps, deprecation repairs, and README metadata upkeep.
    Changes are validated locally and proposed with ``gh pr create``. The
    default branch is never pushed to.

Phase 3 -- Feature and dynamic skills
    Proposes complementary features on ``feature/<slug>`` branches and
    expands skill coverage on demand through :mod:`skill_manager` (Open
    Skills CLI discovery, sandboxed staging, and validated ``SKILL.md``
    generation).

Phase 4 -- Autonomous curation
    Runs the repository's own daily expansion recipe from :mod:`curators`,
    selected through the ``curator.recipe`` block in ``repos.json``. Each
    repository has a bespoke recipe (website templates, knowledge entries,
    trending-repo harvests, a daily briefing, a tool ingest). The work is
    committed to ``chore/daily-curation-<date>`` and proposed with
    ``gh pr create``; a recipe that fails its own post-condition check is
    discarded rather than shipped.

Safety model
------------
* Observer mode never writes to the target repository.
* Every mutation happens on a throwaway clone inside ``workspace/``.
* ``--dry-run`` performs a full audit inside a temporary directory and
  writes nothing to disk.
* Pushes are hard-guarded: only allow-listed branch prefixes are ever
  pushed and the default branch is rejected by an explicit check.
* Curation preflight (:meth:`curators.base.CurationRecipe.check`) runs
  before any branch is created, and ``verify()`` must come back clean
  before a branch is pushed.

CLI examples
------------
    python3 repo_maintainer.py --check-only
    python3 repo_maintainer.py --mode observer
    python3 repo_maintainer.py --mode pr --repo owner/name --dry-run
    python3 repo_maintainer.py --mode feature --repo owner/name
    python3 repo_maintainer.py --curate --repo owner/name --dry-run
    python3 repo_maintainer.py --list-curators
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_HERE))

import skill_manager  # noqa: E402  local module, resolved through _HERE

try:  # Phase 4 is optional: a broken curator must not disable phases 1-3.
    import curators  # noqa: E402
    from curators.base import CurationError  # noqa: E402

    CURATORS_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - defensive
    curators = None  # type: ignore[assignment]

    class CurationError(RuntimeError):  # noqa: D101 - stand-in for the real type
        """Raised when the curator package cannot be imported at all."""

    CURATORS_IMPORT_ERROR = "{0}: {1}".format(type(exc).__name__, exc)

__all__ = [
    "ConfigError",
    "RepoSettings",
    "Finding",
    "DependencyStatus",
    "RepoAudit",
    "Change",
    "RepoOutcome",
    "GitWorkspace",
    "Maintainer",
    "load_config",
    "select_repositories",
    "run_diagnostics",
    "render_curation_pr_body",
    "main",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "repos.json"
WORKSPACE_DIRNAME = "workspace"
REPORTS_DIRNAME = "reports"
LOGS_DIRNAME = "logs"

MODE_OBSERVER = "observer"
MODE_PR = "pr"
MODE_FEATURE = "feature"
MODE_CURATE = "curate"
ALL_MODES = (MODE_OBSERVER, MODE_PR, MODE_FEATURE, MODE_CURATE)

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
SEVERITY_OK = "ok"
SEVERITY_ORDER = (SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_INFO, SEVERITY_OK)

#: Relative directory names never treated as "missing" during a repo scan.
IGNORED_DIR_NAMES = frozenset(
    {".git", "node_modules", "vendor", "dist", "build", "target", ".venv", "venv"}
)

#: Branch name prefixes the engine is ever permitted to create or push.
ALLOWED_BRANCH_PREFIXES = (
    "chore/daily-maintenance-",
    "chore/daily-curation-",
    "feature/",
    "docs/",
    "fix/",
)

MAINTENANCE_BRANCH_TEMPLATE = "chore/daily-maintenance-{date}"
CURATION_BRANCH_TEMPLATE = "chore/daily-curation-{date}"
FEATURE_BRANCH_TEMPLATE = "feature/{slug}"

DEFAULT_CLONE_DEPTH = 30
DEFAULT_GIT_TIMEOUT = 300
DEFAULT_COMMAND_TIMEOUT = 120
DEFAULT_TEST_TIMEOUT = 900
DEFAULT_MAX_BUMPS = 10
MAX_LINK_FILES = 40
MAX_LINK_TARGETS = 200

MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdx")
IMAGE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf", ".zip",
)

#: Directories that indicate a repository has a real automated test suite.
TEST_DIR_NAMES = ("test", "tests", "spec", "specs", "__tests__", "e2e")
#: Files that indicate a repository has a real automated test suite.
TEST_FILE_HINTS = ("test_", "_test.", ".test.", ".spec.")
#: Manifest files used to identify a repository's package manager.
MANIFEST_FILES = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Pipfile",
    "poetry.lock",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "composer.json",
)

#: Ecosystems this engine knows how to audit for outdated dependencies.
KNOWN_ECOSYSTEMS = {
    "npm": "package.json",
    "python": "pyproject.toml",
    "pip": "requirements.txt",
    "go": "go.mod",
    "cargo": "Cargo.toml",
    "rubygems": "Gemfile",
    "maven": "pom.xml",
}

#: Maps a file suffix to a coarse language label for the health report.
_LANGUAGE_BY_SUFFIX = {
    ".py": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".java": "Java",
    ".kt": "Kotlin",
    ".php": "PHP",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cs": "C#",
    ".swift": "Swift",
    ".sh": "Shell",
    ".md": "Markdown",
}

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_GITHUB_REPO_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_MARKDOWN_LINK_RE = re.compile(
    r"\[(?P<label>[^\]]*)\]\((?P<target><[^>]+>|[^)\s]+)(?:\s+\"[^\"]*\")?\)"
)

#: Safe, purely textual deprecation repairs. Each entry is
#: ``(old, new, ecosystems)`` and is only applied when ``old`` is present.
DEPRECATION_REPAIRS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (
        "from distutils.core import setup",
        "from setuptools import setup",
        ("python", "pip"),
    ),
    (
        "distutils.core.setup(",
        "setuptools.setup(",
        ("python", "pip"),
    ),
    (
        "DateTime.now().toISOString()",
        "new Date().toISOString()",
        ("npm",),
    ),
)

# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

#: Re-exported so every subprocess in this module shares one execution policy.
run = skill_manager.run


class ConfigError(RuntimeError):
    """Raised when ``repos.json`` is missing, malformed, or inconsistent."""


class MaintenanceError(RuntimeError):
    """Raised when a guardrail is violated and a phase must abort."""


_VERBOSE = False


def set_verbose(value: bool) -> None:
    """Enable or disable debug-level console output."""
    global _VERBOSE
    _VERBOSE = bool(value)


def _log(message: str) -> None:
    print("[repo-maintainer] {0}".format(message), file=sys.stderr)


def debug(message: str) -> None:
    """Emit a message only when ``--verbose`` is active."""
    if _VERBOSE:
        _log("debug: {0}".format(message))


def slugify(value: str, *, max_length: int = 60, fallback: str = "repo") -> str:
    """Lowercase, hyphen-joined slug safe for a filename or branch segment."""
    slug = _SLUG_STRIP.sub("-", (value or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or fallback


def repo_slug(name: str) -> str:
    """Filesystem-safe slug for a repository name (``owner/repo`` -> ``owner-repo``)."""
    return slugify(name.replace("/", "-"), fallback="repo")


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def today_iso() -> str:
    """Today's date as ``YYYY-MM-DD``."""
    return utcnow().strftime("%Y-%m-%d")


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse a GitHub/git ISO-8601 timestamp into an aware ``datetime``."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def days_since(moment: Optional[datetime], *, reference: Optional[datetime] = None) -> Optional[int]:
    """Whole days elapsed since ``moment`` (``None`` when unknown)."""
    if moment is None:
        return None
    ref = reference or utcnow()
    return max(0, (ref - moment).days)


def humanize_days(count: Optional[int]) -> str:
    """Render a day count as a compact human string."""
    if count is None:
        return "unknown"
    if count == 0:
        return "today"
    if count == 1:
        return "1 day ago"
    if count < 45:
        return "{0} days ago".format(count)
    if count < 365:
        return "{0} months ago".format(round(count / 30.0))
    return "{0} years ago".format(round(count / 365.0, 1))


def truncate(text: str, limit: int = 160) -> str:
    """Collapse whitespace and clip ``text`` to ``limit`` characters."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "\u2026"


def code_fence(text: str, language: str = "") -> str:
    """Wrap ``text`` in a fenced block, lengthening the fence when required."""
    body = text or ""
    longest = 0
    for line in body.splitlines():
        if line.strip().startswith("```"):
            longest = max(longest, len(line.strip()))
    fence = "`" * max(3, longest + 1)
    return "{0}{1}\n{2}\n{0}".format(fence, language, body)


def is_sha(value: str) -> bool:
    """Return ``True`` when ``value`` looks like an abbreviated git SHA."""
    return bool(_SHA_RE.match((value or "").strip().lower()))


def parse_github_slug(value: str) -> Optional[Tuple[str, str]]:
    """Extract ``(owner, repo)`` from a GitHub URL or ``owner/repo`` string."""
    match = _GITHUB_REPO_RE.match((value or "").strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo")


def which(binary: str) -> Optional[str]:
    """Locate ``binary`` on ``PATH`` without raising."""
    return shutil.which(binary)


def relative_to_cwd(path: Path) -> str:
    """Render ``path`` relative to this module's directory when possible."""
    try:
        return str(Path(path).resolve().relative_to(ROOT_DIR))
    except (ValueError, OSError):
        return str(path)


# --------------------------------------------------------------------------- #
# Configuration layer
# --------------------------------------------------------------------------- #


def _deep_merge(base: Any, override: Any) -> Dict[str, Any]:
    """Recursively merge ``override`` over ``base`` without mutating either."""
    result: Dict[str, Any] = dict(base) if isinstance(base, dict) else {}
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _as_int(value: Any, default: int) -> int:
    """Coerce ``value`` to ``int``, falling back to ``default``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class RepoSettings:
    """Fully resolved settings for a single repository.

    Per-repository blocks in ``repos.json`` are deep-merged over ``defaults``
    so a repository only needs to declare what it wants to override.
    """

    def __init__(self, raw: Dict[str, Any], defaults: Dict[str, Any]) -> None:
        merged: Dict[str, Any] = {}
        for group in sorted(set(defaults) | set(raw)):
            if group in ("name", "url", "notes"):
                continue
            base = defaults.get(group, {})
            override = raw.get(group, {})
            merged[group] = _deep_merge(base, override) if isinstance(base, dict) else override

        self.raw = dict(raw)
        self.name = str(raw.get("name") or "").strip()
        self.url = str(raw.get("url") or "").strip()
        self.notes = str(raw.get("notes") or "").strip()
        self.enabled = bool(raw.get("enabled", True))
        self._default_branch = raw.get("default_branch")
        self.clone_depth = max(1, _as_int(merged.get("clone_depth"), DEFAULT_CLONE_DEPTH))
        self.modes: Dict[str, bool] = {
            mode: bool((merged.get("modes") or {}).get(mode, True)) for mode in ALL_MODES
        }
        self.thresholds: Dict[str, Any] = dict(merged.get("thresholds") or {})
        self.docs: Dict[str, Any] = dict(merged.get("docs") or {})
        self.dependencies: Dict[str, Any] = dict(merged.get("dependencies") or {})
        self.quality: Dict[str, Any] = dict(merged.get("quality") or {})
        self.skills: Dict[str, Any] = dict(merged.get("skills") or {})
        self.labels: Dict[str, str] = {
            str(key): str(value)
            for key, value in (merged.get("labels") or {}).items()
        }
        #: Phase 4 binding: ``{"recipe": ..., "options": {...}}``.
        self.curator: Dict[str, Any] = dict(merged.get("curator") or {})

    # -- accessors --------------------------------------------------------- #

    @property
    def slug(self) -> str:
        """Filesystem-safe identifier for this repository."""
        return repo_slug(self.name or self.url)

    def curator_recipe(self) -> str:
        """Recipe id this repository curates with, or ``""`` when unset.

        A ``curator`` block whose ``enabled`` flag is false is treated as
        absent so an operator can park a recipe without deleting it.
        """
        if not self.curator.get("enabled", True):
            return ""
        return str(self.curator.get("recipe") or "").strip()

    def curator_options(self) -> Dict[str, Any]:
        """Recipe knobs merged over any ``defaults.curator.options``."""
        return dict(self.curator.get("options") or {})

    @property
    def default_branch(self) -> Optional[str]:
        """Configured default branch override, if any."""
        return str(self._default_branch) if self._default_branch else None

    def mode_enabled(self, mode: str) -> bool:
        """Return ``True`` when ``mode`` is allowed for this repository."""
        return bool(self.enabled and self.modes.get(mode, True))

    def threshold(self, key: str, default: Any = None) -> Any:
        """Read a staleness threshold."""
        value = self.thresholds.get(key, default)
        return default if value is None else value

    @property
    def test_timeout(self) -> int:
        """Timeout applied to local test execution, in seconds."""
        return _as_int(self.quality.get("test_timeout_seconds"), DEFAULT_TEST_TIMEOUT)

    @property
    def max_bumps(self) -> int:
        """Upper bound on dependency bumps applied in a single run."""
        return max(0, _as_int(self.dependencies.get("max_bumps_per_run"), DEFAULT_MAX_BUMPS))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<RepoSettings {0} enabled={1}>".format(self.name, self.enabled)


def load_config(path: Path) -> Dict[str, Any]:
    """Load and validate ``repos.json``.

    Raises :class:`ConfigError` for a missing file, invalid JSON, an
    unsupported ``schema_version``, or structurally invalid repositories.
    """
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise ConfigError("config file not found: {0}".format(config_path))
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("cannot read {0}: {1}".format(config_path, exc)) from exc
    try:
        data = json.loads(raw_text)
    except ValueError as exc:
        raise ConfigError("invalid JSON in {0}: {1}".format(config_path, exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError("config root must be a JSON object")

    version = _as_int(data.get("schema_version", 1), 1)
    if version != 1:
        raise ConfigError(
            "unsupported schema_version: {0!r}".format(data.get("schema_version"))
        )

    if not isinstance(data.get("defaults", {}), dict):
        raise ConfigError("'defaults' must be an object")
    repositories = data.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ConfigError("'repositories' must be a non-empty array")

    seen = set()
    for index, entry in enumerate(repositories):
        if not isinstance(entry, dict):
            raise ConfigError("repositories[{0}] must be an object".format(index))
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not name:
            raise ConfigError("repositories[{0}] is missing 'name'".format(index))
        if not url:
            raise ConfigError("repositories[{0}] ({1}) is missing 'url'".format(index, name))
        if not re.match(r"^(https?://|git@|ssh://)", url):
            raise ConfigError(
                "repositories[{0}] ({1}) has an unsupported url: {2}".format(index, name, url)
            )
        if name in seen:
            raise ConfigError("duplicate repository name: {0}".format(name))
        seen.add(name)
        modes = entry.get("modes", {})
        if modes and not isinstance(modes, dict):
            raise ConfigError("repositories[{0}].modes must be an object".format(name))
        for mode in modes or {}:
            if mode not in ALL_MODES:
                raise ConfigError(
                    "repositories[{0}] declares unknown mode '{1}'".format(name, mode)
                )
        curator = entry.get("curator")
        if curator is not None and not isinstance(curator, dict):
            raise ConfigError("repositories[{0}].curator must be an object".format(name))
        options = (curator or {}).get("options")
        if options is not None and not isinstance(options, dict):
            raise ConfigError(
                "repositories[{0}].curator.options must be an object".format(name)
            )
        recipe = str((curator or {}).get("recipe") or "").strip()
        if recipe and curators is not None and recipe not in curators.REGISTRY:
            # Fail at load time rather than midway through a daily run.
            raise ConfigError(
                "repositories[{0}] references unknown curator recipe '{1}'; "
                "known recipes: {2}".format(
                    name, recipe, ", ".join(curators.RECIPE_IDS) or "(none)"
                )
            )
    if curators is None and any(
        str((item.get("curator") or {}).get("recipe") or "").strip()
        for item in repositories
    ):
        raise ConfigError(
            "configuration references curator recipes but the curators package "
            "is unusable: {0}".format(CURATORS_IMPORT_ERROR)
        )
    return data


def select_repositories(
    config: Dict[str, Any], *, repo_filter: Optional[str] = None, mode: Optional[str] = None
) -> Tuple[List[RepoSettings], List[str]]:
    """Resolve configured repositories into settings, honouring both filters.

    Returns ``(settings, notes)`` where ``notes`` explains every repository
    that was skipped so the caller can report them verbatim.
    """
    defaults = config.get("defaults", {}) or {}
    notes: List[str] = []
    selected: List[RepoSettings] = []

    for entry in config.get("repositories", []) or []:
        settings = RepoSettings(entry, defaults)
        if not settings.name:
            continue
        if repo_filter and settings.name != repo_filter:
            # Accept the file-safe slug and the owner/repo form of the URL.
            as_slug = slugify(repo_filter, fallback="")
            as_pair = parse_github_slug(repo_filter)
            if settings.slug == as_slug or (
                as_pair is not None and as_pair == parse_github_slug(settings.url)
            ):
                settings.name = repo_filter
            else:
                continue
        if not settings.enabled:
            notes.append("{0}: disabled in config (skipped)".format(settings.name))
            continue
        if mode and not settings.modes.get(mode, True):
            notes.append(
                "{0}: mode '{1}' disabled in config (skipped)".format(settings.name, mode)
            )
            continue
        if mode == MODE_CURATE and not settings.curator_recipe():
            notes.append("{0}: no curator recipe configured (skipped)".format(settings.name))
            continue
        selected.append(settings)

    if repo_filter and not selected:
        known = ", ".join(
            str(item.get("name")) for item in (config.get("repositories", []) or [])
        )
        if notes:
            raise ConfigError(
                "--repo {0} matched but is unavailable: {1}".format(
                    repo_filter, "; ".join(notes)
                )
            )
        raise ConfigError(
            "--repo {0} not found in config. Known repositories: {1}".format(
                repo_filter, known or "(none)"
            )
        )
    return selected, notes


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Finding:
    """A single audit observation produced by a Phase 1 check."""

    category: str
    severity: str
    title: str
    detail: str = ""
    evidence: str = ""
    suggestion: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dictionary (for JSON output)."""
        return {
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
        }


@dataclass
class DependencyStatus:
    """Outdated-dependency results for a single detected package manager."""

    ecosystem: str
    manifest: str
    manager: str
    total: int = 0
    outdated: List[Dict[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    supported: bool = True

    def by_kind(self, kind: str) -> List[Dict[str, str]]:
        """Return outdated entries whose target version is a ``kind`` bump."""
        return [item for item in self.outdated if item.get("kind") == kind]

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dictionary (for JSON output)."""
        return {
            "ecosystem": self.ecosystem,
            "manifest": self.manifest,
            "manager": self.manager,
            "total": self.total,
            "outdated": list(self.outdated),
            "notes": list(self.notes),
            "supported": self.supported,
        }


@dataclass
class RepoAudit:
    """Aggregated Phase 1 audit result for one repository."""

    name: str
    url: str
    slug: str
    path: Path
    default_branch: str
    head_sha: str = ""
    last_commit_at: Optional[datetime] = None
    last_commit_subject: str = ""
    last_commit_author: str = ""
    commits_in_window: int = 0
    window_days: int = 0
    open_prs: List[Dict[str, Any]] = field(default_factory=list)
    stale_branches: List[Dict[str, Any]] = field(default_factory=list)
    open_issues: int = 0
    language: str = ""
    findings: List[Finding] = field(default_factory=list)
    dependencies: List[DependencyStatus] = field(default_factory=list)
    docs_summary: Dict[str, Any] = field(default_factory=dict)
    test_summary: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def add(
        self,
        category: str,
        severity: str,
        title: str,
        detail: str = "",
        evidence: str = "",
        suggestion: str = "",
    ) -> Finding:
        """Record a finding and return it."""
        finding = Finding(
            category=category,
            severity=severity,
            title=title,
            detail=detail,
            evidence=evidence,
            suggestion=suggestion,
        )
        self.findings.append(finding)
        return finding

    def findings_by_category(self, category: str) -> List[Finding]:
        """Return every finding belonging to ``category``."""
        return [item for item in self.findings if item.category == category]

    def severity_counts(self) -> Dict[str, int]:
        """Count findings per severity, always returning all four buckets."""
        counts = {level: 0 for level in SEVERITY_ORDER}
        for item in self.findings:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        return counts

    def days_since_commit(self) -> Optional[int]:
        """Days elapsed since the most recent commit on the default branch."""
        return days_since(self.last_commit_at)

    def health_score(self) -> int:
        """Derive a 0-100 health score by penalising findings by severity.

        Weights are deliberately steep so one critical issue cannot be
        masked by a long tail of informational noise.
        """
        counts = self.severity_counts()
        penalties = counts[SEVERITY_CRITICAL] * 25
        penalties += counts[SEVERITY_WARNING] * 8
        penalties += counts[SEVERITY_INFO] * 2
        return max(0, min(100, 100 - penalties))

    def grade(self) -> str:
        """Map the health score to a letter grade."""
        score = self.health_score()
        if score >= 90:
            return "A"
        if score >= 75:
            return "B"
        if score >= 55:
            return "C"
        if score >= 35:
            return "D"
        return "F"


@dataclass
class Change:
    """A single non-destructive modification made during Phase 2 or 3."""

    category: str
    path: str
    summary: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dictionary (for JSON output)."""
        return {
            "category": self.category,
            "path": self.path,
            "summary": self.summary,
            "detail": self.detail,
        }


@dataclass
class RepoOutcome:
    """Result of running one phase against one repository."""

    repo: str
    mode: str
    status: str = "ok"  # ok | no-changes | preview | skipped | failed
    messages: List[str] = field(default_factory=list)
    changes: List[Change] = field(default_factory=list)
    report_path: Optional[Path] = None
    branch: Optional[str] = None
    commit: Optional[str] = None
    pr_url: Optional[str] = None
    tests: Dict[str, Any] = field(default_factory=dict)
    skills: List[Dict[str, Any]] = field(default_factory=list)
    proposals: List[str] = field(default_factory=list)
    #: Phase 4 detail: recipe id, preflight checklist, produced items, notes.
    curation: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dictionary (for JSON output)."""
        return {
            "repo": self.repo,
            "mode": self.mode,
            "status": self.status,
            "messages": list(self.messages),
            "changes": [change.to_dict() for change in self.changes],
            "report_path": str(self.report_path) if self.report_path else None,
            "branch": self.branch,
            "commit": self.commit,
            "pr_url": self.pr_url,
            "tests": dict(self.tests),
            "skills": list(self.skills),
            "proposals": list(self.proposals),
            "curation": dict(self.curation),
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# Git workspace with branch guardrails
# --------------------------------------------------------------------------- #


class GitWorkspace:
    """An isolated, throwaway clone used for every read and every write.

    All repository mutations happen here, never in the maintainer's own
    checkout. :meth:`guard_branch` refuses any branch that is not
    allow-listed, which makes ``main``/``master`` unreachable by
    construction.
    """

    def __init__(
        self,
        settings: RepoSettings,
        root: Path,
        *,
        dry_run: bool = False,
        materialise: bool = False,
    ) -> None:
        self.settings = settings
        self.path = Path(root) / settings.slug
        self.url = settings.url
        self.dry_run = bool(dry_run)
        #: Clone for real even during a dry-run. Only safe because the caller
        #: guarantees the root is a throwaway directory (see
        #: :meth:`Maintainer._active_workspace_root`), which is what lets
        #: Phase 4 demonstrate a recipe end to end without touching the
        #: target repository.
        self.materialise = bool(materialise)
        self.default_branch = ""
        self._events: List[str] = []

    # -- logging ----------------------------------------------------------- #

    def log(self, message: str) -> None:
        """Record and emit a workspace-level progress message."""
        self._events.append(message)
        _log("{0}: {1}".format(self.settings.name, message))

    @property
    def events(self) -> List[str]:
        """Every message logged by this workspace so far."""
        return list(self._events)

    # -- git primitives ---------------------------------------------------- #

    def _git(
        self, *args: str, timeout: int = DEFAULT_GIT_TIMEOUT, check: bool = False
    ) -> Tuple[int, str, str]:
        """Run a git command inside the workspace."""
        cmd = ["git", "-C", str(self.path)] + [str(arg) for arg in args]
        code, out, err = run(cmd, timeout=timeout, check=check)
        if code != 0:
            debug("git {0} -> {1}: {2}".format(" ".join(args), code, err.strip()[:200]))
        return code, out, err

    @property
    def exists(self) -> bool:
        """Whether the workspace directory holds a valid git checkout."""
        return (self.path / ".git").exists()

    def clone(self) -> None:
        """Clone the target repository, reusing an existing checkout if present."""
        if self.exists:
            self.log("reusing existing workspace at {0}".format(relative_to_cwd(self.path)))
            self.fetch()
            return
        if self.dry_run and not self.materialise:
            self.log("[dry-run] would clone {0} into {1}".format(self.url, self.path))
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        depth = max(1, int(self.settings.clone_depth or DEFAULT_CLONE_DEPTH))
        code, out, err = run(
            ["git", "clone", "--depth", str(depth), "--no-tags", self.url, str(self.path)],
            timeout=max(DEFAULT_GIT_TIMEOUT, depth * 30),
        )
        if code != 0:
            if self.dry_run:
                # A preview must degrade to a message, never to an exception.
                self.log(
                    "[dry-run] could not clone {0}: {1}".format(
                        self.url, truncate(err or out, 200)
                    )
                )
                return
            raise MaintenanceError(
                "git clone failed for {0}: {1}".format(self.url, err.strip() or out.strip())
            )
        self.log("cloned {0} (depth {1})".format(self.url, depth))

    def fetch(self) -> None:
        """Fetch the latest refs for an existing workspace."""
        if not self.exists or self.dry_run:
            return
        self._git("fetch", "--prune", "--depth", str(self.settings.clone_depth), "origin")

    # -- inspection --------------------------------------------------------- #

    def detect_default_branch(self) -> str:
        """Resolve the default branch, preferring any config override."""
        if self.settings.default_branch:
            self.default_branch = self.settings.default_branch
            return self.default_branch

        code, out, _err = self._git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if code == 0 and out.strip():
            self.default_branch = out.strip().split("/")[-1]
            return self.default_branch

        for candidate in ("main", "master", "trunk", "develop"):
            code, _out, _err = self._git(
                "rev-parse", "--verify", "--quiet", "refs/remotes/origin/{0}".format(candidate)
            )
            if code == 0:
                self.default_branch = candidate
                return candidate
        self.default_branch = "main"
        return self.default_branch


    # -- inspection helpers ------------------------------------------------- #

    def head_sha(self) -> str:
        """Return the current HEAD SHA (empty string when unavailable)."""
        code, out, _err = self._git("rev-parse", "HEAD")
        return out.strip() if code == 0 else ""

    def current_branch(self) -> str:
        """Return the checked-out branch name."""
        code, out, _err = self._git("rev-parse", "--abbrev-ref", "HEAD")
        return out.strip() if code == 0 else ""

    def log_commits(self, limit: int, default_branch: str) -> List[Dict[str, Any]]:
        """Return up to ``limit`` commits on ``default_branch``."""
        fmt = "%H%x1f%aI%x1f%an%x1f%s"
        code, out, _err = self._git(
            "log", "-n", str(max(1, limit)), "--format={0}".format(fmt), default_branch
        )
        if code != 0 or not out.strip():
            return []
        commits: List[Dict[str, Any]] = []
        for line in out.strip().splitlines():
            parts = line.split("\x1f")
            if len(parts) < 4:
                continue
            commits.append(
                {
                    "sha": parts[0].strip(),
                    "date": parts[1].strip(),
                    "author": parts[2].strip(),
                    "subject": parts[3].strip(),
                }
            )
        return commits

    def list_remote_branches(self) -> List[Dict[str, Any]]:
        """Return non-default remote branches with their last commit date."""
        code, out, _err = self._git(
            "for-each-ref",
            "--format=%(refname:short)%x1f%(committerdate:iso-strict)%x1f%(objectname:short)",
            "refs/remotes/origin",
        )
        if code != 0 or not out.strip():
            return []
        branches: List[Dict[str, Any]] = []
        for line in out.strip().splitlines():
            parts = line.split("\x1f")
            if len(parts) < 3:
                continue
            name = parts[0].strip()
            if name.startswith("origin/"):
                name = name[len("origin/") :]
            if not name or name == "HEAD" or name == self.default_branch:
                continue
            branches.append({"name": name, "date": parts[1].strip(), "sha": parts[2].strip()})
        return branches

    def tracked_files(self) -> List[str]:
        """Return every tracked path in the repository."""
        code, out, _err = self._git("ls-files")
        return out.strip().splitlines() if code == 0 and out.strip() else []

    def primary_language(self) -> str:
        """Detect the dominant language from tracked file extensions."""
        weights: Dict[str, int] = {}
        for line in self.tracked_files():
            suffix = Path(line).suffix.lower()
            language = _LANGUAGE_BY_SUFFIX.get(suffix)
            if language:
                weights[language] = weights.get(language, 0) + 1
        if not weights:
            return ""
        return max(weights.items(), key=lambda item: item[1])[0]


    # -- write side (guardrailed) ------------------------------------------ #

    def guard_branch(self, branch: str) -> None:
        """Refuse any branch outside the allow-list.

        This single choke point is what makes "never push to main" a
        structural guarantee rather than a convention. It deliberately says
        nothing about which branch is currently checked out: pushing the
        branch you are standing on is the normal, intended flow, and only
        :meth:`create_branch` has any reason to care about that.
        """
        name = (branch or "").strip()
        if not name:
            raise MaintenanceError("branch name is empty")
        if name in ("main", "master", "trunk", "develop", "HEAD", "origin/HEAD"):
            raise MaintenanceError("refusing to use protected branch '{0}'".format(name))
        if name == self.default_branch:
            raise MaintenanceError("refusing to use default branch '{0}'".format(name))
        if not name.startswith(ALLOWED_BRANCH_PREFIXES):
            raise MaintenanceError(
                "branch '{0}' does not use an allowed prefix ({1})".format(
                    name, ", ".join(ALLOWED_BRANCH_PREFIXES)
                )
            )

    def create_branch(self, branch: str) -> str:
        """Create ``branch`` from the freshly fetched default branch.

        A same-day re-run (a launchd retry, or a manual re-run after fixing a
        failed attempt) finds the reused workspace still sitting on the target
        branch. Stepping back to the default branch first makes the operation
        idempotent: the branch is always rebuilt from ``origin/<default>``,
        never from the previous attempt's leftovers.
        """
        name = (branch or "").strip()
        self.guard_branch(name)
        if self.current_branch() == name:
            self._git("checkout", self.default_branch or "HEAD")
        if self.dry_run:
            self.log("[dry-run] would create branch '{0}'".format(branch))
            return branch
        base = "origin/{0}".format(self.default_branch)
        code, _out, err = self._git("checkout", "-B", branch, base)
        if code != 0:
            code2, _out2, err2 = self._git("checkout", "-B", branch)
            if code2 != 0:
                raise MaintenanceError(
                    "could not create branch '{0}': {1}".format(
                        branch, truncate(err or err2, 200)
                    )
                )
        self.log("created branch '{0}' from {1}".format(branch, base))
        return branch

    def has_changes(self) -> bool:
        """Whether the working tree or index differs from HEAD."""
        code, out, _err = self._git("status", "--porcelain")
        return code == 0 and bool(out.strip())

    def changed_files(self) -> List[str]:
        """List files modified or added relative to HEAD."""
        code, out, _err = self._git("status", "--porcelain")
        if code != 0 or not out.strip():
            return []
        files: List[str] = []
        for line in out.strip().splitlines():
            entry = line[3:].strip() if len(line) > 3 else ""
            if " -> " in entry:  # rename
                entry = entry.split(" -> ")[-1]
            if entry:
                files.append(entry.strip('"'))
        return files

    def diff_stat(self) -> str:
        """Return a short ``git diff --stat`` summary of the work."""
        code, out, _err = self._git("diff", "--stat", "HEAD")
        return out.strip() if code == 0 else ""

    def diff_summary(self) -> List[str]:
        """Return per-file insertion/deletion Markdown rows for the PR body."""
        code, out, _err = self._git("diff", "--numstat", "HEAD")
        if code != 0 or not out.strip():
            return []
        rows: List[str] = []
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, removed, path = parts[0].strip(), parts[1].strip(), parts[2].strip()
            rows.append("| `{0}` | {1} | {2} |".format(path, added or "0", removed or "0"))
        return rows


    def commit_all(self, message: str, *, allow_empty: bool = False) -> str:
        """Stage everything and create a commit; returns the new SHA."""
        if self.dry_run:
            self.log("[dry-run] would commit: {0}".format(truncate(message, 100)))
            return ""
        if not self.has_changes() and not allow_empty:
            return ""
        self._git("add", "-A")
        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        code, out, err = self._git(*args)
        if code != 0 and (
            "Please tell me who you are" in (out + err) or "empty ident" in (out + err)
        ):
            # Common on fresh CI images; set a local bot identity and retry.
            self._git("config", "user.name", "repo-maintainer")
            self._git("config", "user.email", "repo-maintainer@users.noreply.github.com")
            code, out, err = self._git(*args)
        if code != 0:
            raise MaintenanceError("git commit failed: {0}".format(truncate(err or out, 300)))
        sha = self.head_sha()
        subject = message.strip().splitlines()[0] if message.strip() else ""
        self.log("committed {0} ({1})".format(sha[:8], truncate(subject, 70)))
        return sha

    def push_branch(self, branch: str, *, force: bool = False) -> bool:
        """Push ``branch`` to ``origin``, re-asserting the guardrails first.

        Pass ``force=True`` for a branch this engine owns exclusively. Such a
        branch is rebuilt from ``origin/<default>`` on every run, so a
        same-day re-run legitimately rewrites it; ``--force-with-lease`` makes
        that safe by still refusing when the remote carries commits this
        workspace has never seen (i.e. a human pushed to it).
        """
        self.guard_branch(branch)
        if self.dry_run:
            self.log("[dry-run] would push branch '{0}' to origin".format(branch))
            return False
        args = ["push", "--set-upstream"]
        if force:
            args.append("--force-with-lease")
        args += ["origin", "{0}:refs/heads/{0}".format(branch)]
        code, out, err = self._git(*args, timeout=DEFAULT_GIT_TIMEOUT * 2)
        if code != 0:
            raise MaintenanceError(
                "git push failed for '{0}': {1}".format(branch, truncate(err or out, 300))
            )
        self.log("pushed branch '{0}' to origin".format(branch))
        return True

    def reset_hard(self, ref: str = "HEAD") -> None:
        """Discard every working-tree change back to ``ref``."""
        if self.dry_run:
            self.log("[dry-run] would reset --hard {0}".format(ref))
            return
        self._git("reset", "--hard", ref)
        self._git("clean", "-fd")

    def _gh(self, *args: str, timeout: int = DEFAULT_COMMAND_TIMEOUT) -> Tuple[int, str, str]:
        """Run a ``gh`` command scoped to the workspace repository."""
        cmd = ["gh"] + [str(arg) for arg in args] + ["--repo", self.settings.name]
        return run(cmd, cwd=self.path, timeout=timeout)

    def add_labels(self, labels: Sequence[str], branch: str) -> List[str]:
        """Apply labels to a pull request, skipping any the repository lacks.

        Creating labels is deliberately out of scope: a maintenance bot must
        not be able to invent a taxonomy in someone else's repository. A label
        that does not exist yet is reported and ignored, which is also what
        makes a fresh clone of a brand-new repository a no-op rather than an
        error.
        """
        wanted = [str(item).strip() for item in labels if str(item or "").strip()]
        if not wanted or self.dry_run:
            return []

        code, out, _err = self._gh("label", "list", "--limit", "200", "--json", "name")
        existing: set = set()
        if code == 0:
            try:
                parsed = json.loads(out or "[]")
                existing = {
                    str(item.get("name")) for item in parsed if isinstance(item, dict)
                }
            except (ValueError, AttributeError, TypeError):
                existing = set()
        else:
            self.log("could not list labels; skipping labelling")

        usable = [name for name in wanted if name in existing]
        skipped = [name for name in wanted if name not in existing]
        if skipped:
            self.log(
                "label(s) not present in the repository, skipped: {0}".format(
                    ", ".join(skipped)
                )
            )
        if not usable:
            return []

        code, out, err = self._gh("pr", "edit", branch, "--add-label", ",".join(usable))
        if code != 0:
            self.log("could not apply label(s): {0}".format(truncate(err or out, 160)))
            return []
        self.log("applied label(s) to '{0}': {1}".format(branch, ", ".join(usable)))
        return usable


# --------------------------------------------------------------------------- #
# Phase 1 helpers: file discovery and GitHub metadata
# --------------------------------------------------------------------------- #


def iter_repo_files(root: Path, *, limit: Optional[int] = None) -> Iterable[Path]:
    """Yield every non-ignored file under ``root`` (deterministic order)."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIR_NAMES)
        for filename in sorted(filenames):
            yield Path(dirpath) / filename
            count += 1
            if limit is not None and count >= limit:
                return


def find_first(root: Path, names: Sequence[str]) -> Optional[Path]:
    """Return the first existing file matching any name in ``names``."""
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def read_text_safe(path: Path, limit: int = 400_000) -> str:
    """Read a text file, tolerating encoding errors and skipping huge files."""
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def gh_json(args: Sequence[str], cwd: Optional[Path] = None) -> Optional[Any]:
    """Run a ``gh --json`` query and parse the response, or ``None`` on error."""
    cmd = ["gh"] + [str(item) for item in args]
    code, out, err = run(cmd, cwd=cwd, timeout=DEFAULT_COMMAND_TIMEOUT)
    if code != 0 or not out.strip():
        debug("gh {0} failed: {1}".format(" ".join(str(a) for a in args), truncate(err, 200)))
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def gh_auth_ok() -> Tuple[bool, str]:
    """Check whether the GitHub CLI is installed and authenticated."""
    if not which("gh"):
        return False, "gh CLI not found on PATH"
    code, out, err = run(["gh", "auth", "status"], timeout=DEFAULT_COMMAND_TIMEOUT)
    if code == 0:
        account = ""
        for line in out.splitlines():
            if "Logged in to" in line:
                account = line.split("account")[-1].strip() if "account" in line else ""
        return True, "authenticated{0}".format(" as {0}".format(account) if account else "")
    detail = truncate(err or out, 200)
    return False, "gh not authenticated: {0}".format(detail)


# --------------------------------------------------------------------------- #
# Phase 1: audit checks
# --------------------------------------------------------------------------- #


def check_staleness(audit: RepoAudit, ws: GitWorkspace, settings: RepoSettings) -> None:
    """Audit commit recency, stale branches, and stale open pull requests."""
    stale_days = _as_int(settings.threshold("stale_days", 90), 90)
    warn_days = _as_int(settings.threshold("warn_stale_days", 30), 30)
    stale_pr_days = _as_int(settings.threshold("stale_pr_days", 30), 30)
    stale_branch_days = _as_int(settings.threshold("stale_branch_days", 60), 60)

    commits = ws.log_commits(max(10, settings.clone_depth), audit.default_branch)
    audit.commits_in_window = len(commits)
    if not commits:
        audit.add(
            "staleness",
            SEVERITY_WARNING,
            "No commit history available",
            "The shallow clone contains no commits for '{0}'.".format(audit.default_branch),
            suggestion="Increase 'clone_depth' or verify the default branch name.",
        )
        return

    latest = commits[0]
    audit.last_commit_at = parse_timestamp(latest.get("date"))
    audit.last_commit_subject = latest.get("subject", "")
    audit.last_commit_author = latest.get("author", "")

    oldest = parse_timestamp(commits[-1].get("date"))
    if audit.last_commit_at and oldest:
        audit.window_days = max(0, (audit.last_commit_at - oldest).days)

    age = audit.days_since_commit()
    if age is not None and age >= stale_days:
        audit.add(
            "staleness",
            SEVERITY_CRITICAL,
            "Repository is stale",
            "The last commit on '{0}' was {1} days ago (threshold: {2}).".format(
                audit.default_branch, age, stale_days
            ),
            evidence=truncate(audit.last_commit_subject, 120),
            suggestion="Confirm whether the project is maintained; archive if abandoned.",
        )
    elif age is not None and age >= warn_days:
        audit.add(
            "staleness",
            SEVERITY_WARNING,
            "Low commit velocity",
            "The last commit was {0} days ago (warning threshold: {1}).".format(
                age, warn_days
            ),
            evidence=truncate(audit.last_commit_subject, 120),
            suggestion="Check for unaddressed issues or release the accumulated fixes.",
        )
    else:
        audit.add(
            "staleness",
            SEVERITY_OK,
            "Recently active",
            "Last commit {0} by {1}.".format(
                humanize_days(age), audit.last_commit_author or "unknown author"
            ),
            evidence=truncate(audit.last_commit_subject, 120),
        )

    if audit.window_days > 0 and len(commits) >= 5:
        per_week = (len(commits) / audit.window_days) * 7.0
        if per_week < 0.5:
            audit.add(
                "staleness",
                SEVERITY_INFO,
                "Sparse commit history",
                "About {0} commits/week across the last {1} days.".format(
                    round(per_week, 2), audit.window_days
                ),
                suggestion="Consider milestone hygiene and issue triage.",
            )

    # -- stale branches ----------------------------------------------------- #
    for branch in ws.list_remote_branches():
        branch_age = days_since(parse_timestamp(branch.get("date")))
        if branch_age is not None and branch_age >= stale_branch_days:
            audit.stale_branches.append(dict(branch, age_days=branch_age))
    if audit.stale_branches:
        audit.add(
            "staleness",
            SEVERITY_WARNING,
            "Stale branches on the remote",
            "{0} branch(es) have had no commits for {1}+ days.".format(
                len(audit.stale_branches), stale_branch_days
            ),
            evidence="; ".join(
                "{0} ({1}d)".format(item["name"], item["age_days"])
                for item in audit.stale_branches[:10]
            ),
            suggestion="Delete merged branches and confirm long-lived ones are still active.",
        )


    # -- open pull requests ------------------------------------------------- #
    payload = gh_json(
        [
            "pr",
            "list",
            "--repo",
            settings.name,
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            "number,title,createdAt,updatedAt,isDraft,author",
        ]
    )
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                audit.open_prs.append(item)
        stale_prs = []
        for pr in audit.open_prs:
            pr_age = days_since(parse_timestamp(pr.get("updatedAt") or pr.get("createdAt")))
            pr["age_days"] = pr_age
            if pr_age is not None and pr_age >= stale_pr_days:
                stale_prs.append(pr)
        if stale_prs:
            audit.add(
                "staleness",
                SEVERITY_WARNING,
                "Stale open pull requests",
                "{0} of {1} open PRs have had no update for {2}+ days.".format(
                    len(stale_prs), len(audit.open_prs), stale_pr_days
                ),
                evidence="; ".join(
                    "#{0} {1}".format(pr.get("number"), truncate(pr.get("title"), 60))
                    for pr in stale_prs[:10]
                ),
                suggestion="Request re-review, close them, or convert blockers to issues.",
            )
        elif audit.open_prs:
            audit.add(
                "staleness",
                SEVERITY_OK,
                "Pull request queue is fresh",
                "{0} open PR(s), all updated within {1} days.".format(
                    len(audit.open_prs), stale_pr_days
                ),
            )
    else:
        audit.warnings.append(
            "Could not query open pull requests (gh unavailable or unauthenticated)."
        )

    counter = gh_json(
        [
            "api",
            "search/issues",
            "-f",
            "q=repo:{0} is:issue is:open".format(settings.name),
            "-f",
            "per_page=1",
        ]
    )
    if isinstance(counter, dict):
        audit.open_issues = _as_int((counter.get("total_count") or 0), 0)


def _strip_code_fences(text: str) -> str:
    """Remove fenced and indented code blocks before link extraction."""
    without_fences = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    without_inline = re.sub(r"`[^`\n]*`", "", without_fences)
    return "\n".join(line for line in without_inline.splitlines() if not line.startswith("    "))


def check_documentation(audit: RepoAudit, ws: GitWorkspace, settings: RepoSettings) -> None:
    """Audit README presence, badges, setup guides, and internal links."""
    root = ws.path
    docs_cfg = settings.docs
    tracked = set(ws.tracked_files())

    readme = find_first(root, ["README.md", "README.rst", "README.txt", "README", "readme.md"])
    readme_rel = ""
    if readme is not None:
        try:
            readme_rel = str(readme.relative_to(root))
        except ValueError:
            readme_rel = readme.name
    audit.docs_summary["readme"] = readme_rel or None

    # -- README presence ----------------------------------------------------- #
    if bool(docs_cfg.get("require_readme", True)):
        if readme is None:
            audit.add(
                "docs",
                SEVERITY_CRITICAL,
                "No README",
                "The repository has no README at its root, so GitHub shows no landing page.",
                suggestion="Add a README.md with a one-line description and usage examples.",
            )
        else:
            audit.add(
                "docs",
                SEVERITY_OK,
                "README present",
                "Found '{0}' ({1} lines).".format(
                    readme_rel, len(read_text_safe(readme).splitlines())
                ),
            )

    if readme is None:
        audit.docs_summary.update({"badges": 0, "setup_guide": None, "broken_links": 0})
        return

    readme_text = read_text_safe(readme)

    # -- badges -------------------------------------------------------------- #
    badge_count = 0
    badge_kinds: List[str] = []
    if readme is not None:
        badge_text = readme_text[:4000]
        for kind, pattern in (
            ("ci", r"(build|ci)/actions|travis|appveyor|circleci|gitlab-ci|badge.*workflow"),
            ("coverage", r"codecov|coveralls|coverage"),
            ("license", r"licenses/|license.*badge|spdx"),
            ("version", r"badge.*version|shields\.io.*version|npmjs\.com/badge"),
            ("python", r"pypi\.org.*badge|python.*versions"),
            ("node", r"nodei\.co|npmjs\.com/badge|node.*version"),
        ):
            if re.search(pattern, badge_text, re.IGNORECASE):
                badge_kinds.append(kind)
        badge_count = len(re.findall(r"!\[[^\]]*\]\([^)]*\)", badge_text))
    audit.docs_summary["badges"] = badge_count
    audit.docs_summary["badge_kinds"] = badge_kinds

    if bool(docs_cfg.get("require_badges", True)):
        if badge_count == 0:
            audit.add(
                "docs",
                SEVERITY_WARNING,
                "No status badges",
                "The README shows no build, coverage, or release badges.",
                suggestion="Add a CI badge and a release badge to signal project health.",
            )
        elif not {"ci"} & set(badge_kinds):
            audit.add(
                "docs",
                SEVERITY_INFO,
                "No CI badge detected",
                "{0} badge(s) found, but none indicate a CI status.".format(badge_count),
                suggestion="Add a build-status badge linking to the workflow run.",
            )
        else:
            audit.add(
                "docs",
                SEVERITY_OK,
                "Status badges present",
                "{0} badge(s): {1}.".format(badge_count, ", ".join(badge_kinds)),
            )

    # -- setup / contributing guide ------------------------------------------ #
    setup_globs = list(
        docs_cfg.get(
            "setup_guide_globs",
            ["CONTRIBUTING.md", "docs/CONTRIBUTING.md", "SETUP.md", "INSTALL.md"],
        )
    )
    guide = next(
        (name for name in setup_globs if (root / name).is_file() and name in tracked), None
    )
    if guide is None:
        # Fall back to a case-insensitive scan before declaring it missing.
        lowered = {name.lower() for name in tracked}
        guide = next((name for name in setup_globs if name.lower() in lowered), None)
    audit.docs_summary["setup_guide"] = guide
    if bool(docs_cfg.get("require_setup_guide", True)):
        if guide is None:
            audit.add(
                "docs",
                SEVERITY_WARNING,
                "No setup or contributing guide",
                "None of the expected guides exist ({0}).".format(
                    ", ".join(setup_globs[:4])
                ),
                suggestion="Add CONTRIBUTING.md describing local setup, tests, and PR rules.",
            )
        else:
            audit.add("docs", SEVERITY_OK, "Setup guide present", "Found '{0}'.".format(guide))


    # -- broken relative links ----------------------------------------------- #
    if not bool(docs_cfg.get("check_links", True)):
        audit.docs_summary["broken_links"] = 0
        return

    skip_prefixes = tuple(
        str(item).lower() for item in (docs_cfg.get("skip_link_prefixes") or ["#", "mailto:"])
    )
    skip_suffixes = tuple(
        str(item).lower() for item in (docs_cfg.get("skip_link_suffixes") or IMAGE_SUFFIXES)
    )
    skip_domains = {str(item).lower() for item in (docs_cfg.get("skip_link_domains") or [])}

    markdown_files = [
        path
        for path in sorted(root.glob("*.md"))
        if path.is_file()
    ]
    for extra in ("docs", "doc", ".github"):
        subdir = root / extra
        if subdir.is_dir():
            markdown_files.extend(sorted(p for p in subdir.rglob("*.md") if p.is_file()))
    markdown_files = markdown_files[:MAX_LINK_FILES]

    broken: List[str] = []
    checked = 0
    for md_file in markdown_files:
        text = read_text_safe(md_file)
        if not text:
            continue
        base_dir = md_file.parent
        for match in _MARKDOWN_LINK_RE.finditer(_strip_code_fences(text)):
            target = match.group("target").strip().strip("<>")
            if not target or target.lower().startswith(skip_prefixes):
                continue
            if target.lower().startswith(("http://", "https://", "//")):
                if any(domain in target.lower() for domain in skip_domains):
                    continue
                continue  # remote links are out of scope for a local audit
            if target.lower().startswith(skip_suffixes):
                continue
            path_part = target.split("#", 1)[0].split("?", 1)[0]
            if not path_part:
                continue
            checked += 1
            if checked > MAX_LINK_TARGETS:
                break
            candidate = (base_dir / path_part).resolve()
            if candidate.exists():
                continue
            # Retry tolerating a missing .md suffix, a common authoring slip.
            alt = candidate.with_name(candidate.name + ".md")
            if alt.exists():
                continue
            try:
                rel = str(md_file.relative_to(root))
            except ValueError:
                rel = md_file.name
            broken.append("{0} -> {1}".format(rel, target))
        if checked > MAX_LINK_TARGETS:
            break

    audit.docs_summary["broken_links"] = len(broken)
    audit.docs_summary["links_checked"] = checked
    audit.docs_summary["markdown_files"] = len(markdown_files)
    if broken:
        audit.add(
            "docs",
            SEVERITY_WARNING,
            "Broken relative links",
            "{0} broken link(s) across {1} Markdown file(s).".format(
                len(broken), len(markdown_files)
            ),
            evidence="; ".join(broken[:8]),
            suggestion="Fix the target paths or remove links to files that no longer exist.",
        )
    else:
        audit.add(
            "docs",
            SEVERITY_OK,
            "Internal links resolve",
            "Checked {0} relative link(s) in {1} Markdown file(s).".format(
                checked, len(markdown_files)
            ),
        )


# --------------------------------------------------------------------------- #
# Phase 1: dependency freshness
# --------------------------------------------------------------------------- #


def parse_version(value: str) -> Tuple[int, int, int]:
    """Parse a dotted version into a numeric triple, tolerating suffixes."""
    match = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", (value or "").strip())
    if not match:
        return (0, 0, 0)
    return (
        int(match.group(1)),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
    )


def classify_bump(current: str, latest: str) -> str:
    """Classify a version change as ``patch``, ``minor``, ``major``, or ``none``."""
    cur = parse_version(current)
    nxt = parse_version(latest)
    if nxt == cur or nxt < cur:
        return "none"
    if nxt[0] != cur[0]:
        return "major"
    if nxt[1] != cur[1]:
        return "minor"
    if nxt[2] != cur[2]:
        return "patch"
    return "none"


def detect_manifests(root: Path) -> List[Path]:
    """Return every recognised dependency manifest in ``root``."""
    found: List[Path] = []
    for name in MANIFEST_FILES:
        candidate = root / name
        if candidate.is_file():
            found.append(candidate)
    return found


def _npm_outdated(ws: GitWorkspace) -> List[Dict[str, str]]:
    """Run ``npm outdated --json`` and normalise its output."""
    code, out, _err = run(
        ["npm", "outdated", "--json", "--long"],
        cwd=ws.path,
        timeout=DEFAULT_COMMAND_TIMEOUT * 3,
    )
    if code not in (0, 1) or not out.strip():
        return []
    try:
        payload = json.loads(out)
    except ValueError:
        return []
    results: List[Dict[str, str]] = []
    for name, info in (payload or {}).items():
        if not isinstance(info, dict):
            continue
        current = str(info.get("current") or "")
        latest = str(info.get("latest") or "")
        kind = classify_bump(current, latest)
        if kind == "none" and not current:
            kind = "added"
        results.append(
            {
                "name": str(name),
                "current": current or "(not installed)",
                "latest": latest,
                "kind": kind,
                "type": "dev" if info.get("type") == "dev" else "prod",
                "direct": "true",
            }
        )
    return results


def _pip_outdated(ws: GitWorkspace) -> List[Dict[str, str]]:
    """Run ``pip list --outdated --format=json`` and normalise its output."""
    code, out, _err = run(
        [sys.executable, "-m", "pip", "list", "--outdated", "--format=json", "--disable-pip-version-check"],
        cwd=ws.path,
        timeout=DEFAULT_COMMAND_TIMEOUT * 3,
    )
    if code != 0 or not out.strip():
        return []
    try:
        payload = json.loads(out)
    except ValueError:
        return []
    results: List[Dict[str, str]] = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        current = str(item.get("version") or "")
        latest = str(item.get("latest_version") or "")
        if not name or not latest:
            continue
        results.append(
            {
                "name": name,
                "current": current,
                "latest": latest,
                "kind": classify_bump(current, latest),
                "type": "prod",
                "direct": "unknown",
            }
        )
    return results


def check_dependencies(audit: RepoAudit, ws: GitWorkspace, settings: RepoSettings) -> None:
    """Identify package managers and report outdated dependencies."""
    deps_cfg = settings.dependencies
    manifests = detect_manifests(ws.path)
    audit.dependencies = []

    if not bool(deps_cfg.get("enabled", True)):
        return

    if not manifests:
        audit.add(
            "dependencies",
            SEVERITY_INFO,
            "No dependency manifest",
            "No package.json, pyproject.toml, requirements.txt, or equivalent found.",
        )
        return

    for manifest in manifests:
        name = manifest.name
        if name == "package.json":
            status = DependencyStatus(ecosystem="npm", manifest=name, manager="npm")
            if not which("npm"):
                status.supported = False
                status.notes.append("npm is not installed; skipping the outdated check.")
            else:
                status.outdated = _npm_outdated(ws)
                try:
                    payload = json.loads(read_text_safe(manifest) or "{}")
                    status.total = len((payload or {}).get("dependencies", {})) + len(
                        (payload or {}).get("devDependencies", {})
                    )
                except ValueError:
                    pass
        elif name in ("requirements.txt", "Pipfile", "poetry.lock", "pyproject.toml"):
            status = DependencyStatus(ecosystem="python", manifest=name, manager="pip")
            status.outdated = _pip_outdated(ws)
            status.total = len(
                [line for line in read_text_safe(manifest).splitlines() if line.strip()]
            )
            status.notes.append(
                "Resolved against the current interpreter, not the project's pinned environment."
            )
        else:
            ecosystem = KNOWN_ECOSYSTEMS.get(name, "unknown")
            status = DependencyStatus(ecosystem=ecosystem, manifest=name, manager=ecosystem)
            status.supported = False
            status.notes.append(
                "No automated outdated check is implemented for '{0}'.".format(name)
            )

        if status.total and not status.outdated and status.supported:
            status.notes.append("All {0} declared dependencies are current.".format(status.total))
        audit.dependencies.append(status)

    _summarise_dependencies(audit, settings)


def _summarise_dependencies(audit: RepoAudit, settings: RepoSettings) -> None:
    """Fold per-ecosystem results into a single dependency finding."""
    check = bool(settings.dependencies.get("check_outdated", True))
    total_outdated = sum(len(status.outdated) for status in audit.dependencies)
    majors = [item for status in audit.dependencies for item in status.by_kind("major")]
    minors = [item for status in audit.dependencies for item in status.by_kind("minor")]
    patches = [item for status in audit.dependencies for item in status.by_kind("patch")]

    evidence = "; ".join(
        "{0}: {1}".format(item["name"], item["latest"]) for item in (majors + minors)[:8]
    )

    if not check:
        audit.add(
            "dependencies",
            SEVERITY_INFO,
            "Dependency check disabled",
            "'dependencies.check_outdated' is false for this repository.",
        )
        return

    if total_outdated == 0:
        if any(status.supported for status in audit.dependencies):
            audit.add(
                "dependencies",
                SEVERITY_OK,
                "Dependencies are current",
                "No outdated direct dependencies were found across {0} manifest(s).".format(
                    len(audit.dependencies)
                ),
            )
        return

    if majors:
        severity = (
            SEVERITY_CRITICAL if bool(settings.dependencies.get("outdated_major_is_urgent", True))
            else SEVERITY_WARNING
        )
        audit.add(
            "dependencies",
            severity,
            "{0} major version(s) behind".format(len(majors)),
            "Major bumps can contain breaking changes and need a deliberate migration.",
            evidence=evidence,
            suggestion="Schedule a migration; do not auto-bump majors.",
        )
    if minors:
        audit.add(
            "dependencies",
            SEVERITY_WARNING,
            "{0} minor version(s) behind".format(len(minors)),
            "Minor releases usually add features without breaking changes.",
            evidence=evidence,
            suggestion="Batch these into a single reviewed upgrade PR.",
        )
    if patches:
        audit.add(
            "dependencies",
            SEVERITY_INFO,
            "{0} patch version(s) behind".format(len(patches)),
            "Patch releases are low risk and are safe to bump automatically.",
            evidence=evidence,
            suggestion="Phase 2 can bump these automatically when configured.",
        )
    if not (majors or minors or patches):
        audit.add(
            "dependencies",
            SEVERITY_INFO,
            "{0} dependency update(s) available".format(total_outdated),
            "Updates were detected but none classify as patch, minor, or major.",
        )


# --------------------------------------------------------------------------- #
# Phase 1: test and CI coverage
# --------------------------------------------------------------------------- #


def detect_ci_workflows(root: Path) -> List[Path]:
    """Return every GitHub Actions workflow YAML file."""
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    return [
        path
        for path in sorted(workflows.iterdir())
        if path.is_file() and path.suffix in (".yml", ".yaml")
    ]


def detect_test_files(root: Path) -> Tuple[List[str], List[str]]:
    """Return ``(test_files, test_dirs)`` discovered in ``root``."""
    test_files: List[str] = []
    test_dirs: List[str] = []
    for path in iter_repo_files(root):
        if not path.is_file():
            continue
        in_test_dir = bool(set(path.parts) & set(TEST_DIR_NAMES))
        named_like_test = any(hint in path.name for hint in TEST_FILE_HINTS)
        if not (in_test_dir or named_like_test):
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = path.name
        test_files.append(rel)
        parent = "" if path.parent == root else str(path.parent.relative_to(root))
        if in_test_dir and parent and parent not in test_dirs:
            test_dirs.append(parent)
    return test_files, test_dirs


def detect_test_command(root: Path) -> Optional[Tuple[str, List[str]]]:
    """Infer the most likely local test command for ``root``."""
    if (root / "package.json").is_file():
        try:
            payload = json.loads(read_text_safe(root / "package.json") or "{}")
            if "test" in ((payload or {}).get("scripts") or {}):
                return "npm test", ["npm", "test"]
        except ValueError:
            pass
    if (root / "pyproject.toml").is_file() and "pytest" in read_text_safe(root / "pyproject.toml"):
        return "pytest", [sys.executable, "-m", "pytest", "-q"]
    if (root / "Makefile").is_file() and re.search(
        r"^test\s*:", read_text_safe(root / "Makefile"), re.MULTILINE
    ):
        return "make test", ["make", "test"]
    if (root / "go.mod").is_file() and (root / "go.sum").is_file():
        return "go test ./...", ["go", "test", "./..."]
    test_files, _dirs = detect_test_files(root)
    if any(name.startswith("test_") or name.endswith("_test.py") for name in test_files):
        return "python -m pytest", [sys.executable, "-m", "pytest", "-q"]
    return None


def check_tests_and_ci(audit: RepoAudit, ws: GitWorkspace, settings: RepoSettings) -> None:
    """Audit the presence and quality of tests, CI workflows, and coverage."""
    root = ws.path
    workflows = detect_ci_workflows(root)
    test_files, test_dirs = detect_test_files(root)
    command = detect_test_command(root)

    workflow_names = [path.name for path in workflows]
    runs_tests = any(
        re.search(
            r"\b(npm (run )?test|pytest|go test|cargo test|make test|vitest|jest|mocha)\b",
            read_text_safe(workflow),
        )
        for workflow in workflows
    )

    audit.test_summary = {
        "ci_workflows": len(workflows),
        "ci_workflow_names": workflow_names,
        "test_files": len(test_files),
        "test_dirs": test_dirs,
        "test_command": command[0] if command else None,
        "ci_runs_tests": runs_tests,
    }

    # -- CI ------------------------------------------------------------------ #
    if not workflows:
        audit.add(
            "tests-ci",
            SEVERITY_CRITICAL,
            "No GitHub Actions workflows",
            "The repository has no .github/workflows YAML, so nothing runs on push.",
            suggestion="Add a ci.yml that installs dependencies and runs the test suite.",
        )
    else:
        audit.add(
            "tests-ci",
            SEVERITY_OK,
            "CI workflows present",
            "{0} workflow(s): {1}.".format(len(workflows), ", ".join(workflow_names[:5])),
        )
        if runs_tests:
            audit.add(
                "tests-ci",
                SEVERITY_OK,
                "CI executes tests",
                "At least one workflow runs a recognised test command.",
            )
        else:
            audit.add(
                "tests-ci",
                SEVERITY_WARNING,
                "CI never runs the test suite",
                "None of the {0} workflow(s) invoke a recognised test command.".format(
                    len(workflows)
                ),
                suggestion="Add a `test` step so regressions are caught before merge.",
            )

    # -- tests ---------------------------------------------------------------- #
    if not test_files:
        audit.add(
            "tests-ci",
            SEVERITY_CRITICAL,
            "No test suite detected",
            "No test directories or test files were found in the repository.",
            suggestion="Add a minimal test suite and wire it into CI.",
        )
        return

    audit.add(
        "tests-ci",
        SEVERITY_OK,
        "Test suite detected",
        "{0} test file(s) in {1} director(ies); local command: {2}.".format(
            len(test_files), len(test_dirs), command[0] if command else "unknown"
        ),
        evidence=", ".join(test_files[:6]),
    )
    if not command:
        audit.add(
            "tests-ci",
            SEVERITY_WARNING,
            "No standard test command",
            "Tests exist but no package script, pytest config, or Makefile target was found.",
            suggestion="Add a `test` script so contributors and CI run tests the same way.",
        )

    # -- coverage -------------------------------------------------------------- #
    coverage_files = [
        name
        for name in (
            ".codecov.yml",
            "codecov.yml",
            ".coveragerc",
            "pyproject.toml",
            "jest.config.js",
            "vitest.config.ts",
            "nyc.config.js",
        )
        if (root / name).is_file()
    ]
    badge_kinds = audit.docs_summary.get("badge_kinds") or []
    has_coverage_badge = "coverage" in badge_kinds
    audit.test_summary["coverage_config"] = coverage_files
    audit.test_summary["coverage_badge"] = has_coverage_badge
    if not coverage_files and not has_coverage_badge:
        audit.add(
            "tests-ci",
            SEVERITY_INFO,
            "No coverage reporting configured",
            "Neither a coverage config nor a coverage badge was found.",
            suggestion="Add coverage reporting so quality regressions are visible over time.",
        )


# --------------------------------------------------------------------------- #
# Phase 1: report rendering
# --------------------------------------------------------------------------- #

_SEVERITY_ICON = {
    SEVERITY_CRITICAL: "CRITICAL",
    SEVERITY_WARNING: "WARNING",
    SEVERITY_INFO: "INFO",
    SEVERITY_OK: "OK",
}

CATEGORY_TITLES = {
    "staleness": "Commit Staleness",
    "docs": "Documentation Health",
    "dependencies": "Dependency Freshness",
    "tests-ci": "Test and CI Coverage",
}


def _render_findings(findings: Sequence[Finding]) -> List[str]:
    """Render a list of findings as a Markdown table plus detail blocks."""
    if not findings:
        return ["No findings in this category.", ""]
    lines = [
        "| Severity | Finding | Detail |",
        "| --- | --- | --- |",
    ]
    for finding in findings:
        lines.append(
            "| **{0}** | {1} | {2} |".format(
                _SEVERITY_ICON.get(finding.severity, finding.severity.upper()),
                finding.title,
                truncate(finding.detail, 180).replace("|", "\\|") or "-",
            )
        )
    lines.append("")
    actionable = [item for item in findings if item.suggestion]
    if actionable:
        lines.append("**Recommended actions**")
        lines.append("")
        for finding in actionable:
            lines.append("- **{0}**: {1}".format(finding.title, finding.suggestion))
        lines.append("")
    return lines


def render_health_report(audit: RepoAudit, settings: RepoSettings, *, generated_at: str) -> str:
    """Render the full Markdown health report for one repository."""
    counts = audit.severity_counts()
    lines: List[str] = [
        "# Repository Health Report: {0}".format(audit.name),
        "",
        "> Generated by `repo_maintainer.py` in Phase 1 (Observer) on {0}.".format(generated_at),
        "> Observer mode is read-only: no files in this repository were modified.",
        "",
        "## Executive Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        "| Repository | `{0}` |".format(audit.url),
        "| Default branch | `{0}` |".format(audit.default_branch),
        "| Head commit | `{0}` |".format(audit.head_sha[:12] or "unknown"),
        "| Last commit | {0} |".format(humanize_days(audit.days_since_commit())),
        "| Primary language | {0} |".format(audit.language or "unknown"),
        "| Open pull requests | {0} |".format(len(audit.open_prs)),
        "| Open issues | {0} |".format(audit.open_issues),
        "| Stale remote branches | {0} |".format(len(audit.stale_branches)),
        "| Health score | **{0}/100 (grade {1})** |".format(
            audit.health_score(), audit.grade()
        ),
        "",
        "### Finding counts",
        "",
        "| Critical | Warning | Info | OK |",
        "| --- | --- | --- | --- |",
        "| {0} | {1} | {2} | {3} |".format(
            counts[SEVERITY_CRITICAL],
            counts[SEVERITY_WARNING],
            counts[SEVERITY_INFO],
            counts[SEVERITY_OK],
        ),
        "",
        "### Category scorecard",
        "",
        "| Category | Critical | Warning | Info | OK |",
        "| --- | --- | --- | --- | --- |",
    ]
    for category, title in CATEGORY_TITLES.items():
        bucket = counts_from(audit, category)
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} |".format(
                title,
                bucket[SEVERITY_CRITICAL],
                bucket[SEVERITY_WARNING],
                bucket[SEVERITY_INFO],
                bucket[SEVERITY_OK],
            )
        )
    lines.append("")

    priority = [item for item in audit.findings if item.severity in (SEVERITY_CRITICAL, SEVERITY_WARNING)]
    if priority:
        lines.extend(["## Top Priorities", ""])
        for index, finding in enumerate(priority[:5], 1):
            lines.append(
                "{0}. **[{1}] {2}** - {3}".format(
                    index,
                    _SEVERITY_ICON.get(finding.severity, finding.severity),
                    finding.title,
                    finding.detail,
                )
            )
        lines.append("")

    for category, title in CATEGORY_TITLES.items():
        lines.extend(["## {0}".format(title), ""])
        lines.extend(_render_findings(audit.findings_by_category(category)))

    lines.extend(_render_dependency_tables(audit))
    lines.extend(_render_activity_tables(audit))

    if audit.warnings:
        lines.extend(["## Audit Warnings", ""])
        lines.extend("- {0}".format(item) for item in audit.warnings)
        lines.append("")

    lines.extend(
        [
            "## Next Steps",
            "",
            "- Run `--mode pr` to open a guarded maintenance pull request.",
            "- Run `--mode feature` to propose architecture improvements and expand skills.",
            "- Adjust thresholds per repository in `config/repos.json` to tune this audit.",
            "",
        ]
    )
    return "\n".join(lines)


def counts_from(audit: RepoAudit, category: str) -> Dict[str, int]:
    """Count findings per severity within a single category."""
    counts = {level: 0 for level in SEVERITY_ORDER}
    for item in audit.findings_by_category(category):
        counts[item.severity] = counts.get(item.severity, 0) + 1
    return counts


def _render_dependency_tables(audit: RepoAudit) -> List[str]:
    """Render the per-ecosystem outdated dependency tables."""
    if not audit.dependencies:
        return []
    lines: List[str] = ["### Dependency detail", ""]
    for status in audit.dependencies:
        lines.extend(
            [
                "**{0}** (`{1}`, {2} declared)".format(
                    status.ecosystem, status.manifest, status.total
                ),
                "",
            ]
        )
        if status.notes:
            lines.extend("- {0}".format(note) for note in status.notes)
            lines.append("")
        if status.outdated:
            lines.extend(
                [
                    "| Package | Current | Latest | Bump |",
                    "| --- | --- | --- | --- |",
                ]
            )
            for item in status.outdated[:25]:
                lines.append(
                    "| `{0}` | {1} | {2} | {3} |".format(
                        item.get("name"),
                        item.get("current"),
                        item.get("latest"),
                        item.get("kind"),
                    )
                )
            if len(status.outdated) > 25:
                lines.append("")
                lines.append("_...and {0} more._".format(len(status.outdated) - 25))
        lines.append("")
    return lines


def _render_activity_tables(audit: RepoAudit) -> List[str]:
    """Render stale branch and stale pull request detail tables."""
    lines: List[str] = []
    if audit.stale_branches:
        lines.extend(
            [
                "### Stale branches",
                "",
                "| Branch | Last commit | Age (days) |",
                "| --- | --- | --- |",
            ]
        )
        for item in audit.stale_branches[:20]:
            lines.append(
                "| `{0}` | {1} | {2} |".format(
                    item.get("name"),
                    (parse_timestamp(item.get("date")) or utcnow()).strftime("%Y-%m-%d"),
                    item.get("age_days"),
                )
            )
        lines.append("")

    stale_prs = [pr for pr in audit.open_prs if (pr.get("age_days") or 0) >= 30]
    if stale_prs:
        lines.extend(
            [
                "### Stale open pull requests",
                "",
                "| PR | Title | Age (days) | Draft |",
                "| --- | --- | --- | --- |",
            ]
        )
        for pr in stale_prs[:20]:
            lines.append(
                "| #{0} | {1} | {2} | {3} |".format(
                    pr.get("number"),
                    truncate(pr.get("title"), 70).replace("|", "\\|"),
                    pr.get("age_days"),
                    "yes" if pr.get("isDraft") else "no",
                )
            )
        lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# Phase 2: automated PR maintenance
# --------------------------------------------------------------------------- #


def _format_commands(root: Path) -> List[Tuple[str, List[str]]]:
    """Return the formatter commands that apply to this repository."""
    commands: List[Tuple[str, List[str]]] = []
    if (root / "pyproject.toml").is_file() or any(
        root.rglob("*.py")
    ):
        if which("ruff"):
            commands.append(("ruff format", ["ruff", "format", "."]))
        elif which("black"):
            commands.append(("black", ["black", "--quiet", "."]))
    if (root / "package.json").is_file():
        if which("npx"):
            commands.append(("prettier", ["npx", "--yes", "prettier", "--write", "."]))
    if (root / "go.mod").is_file() and which("gofmt"):
        commands.append(("gofmt", ["gofmt", "-w", "-l", "."]))
    if (root / "Cargo.toml").is_file() and which("cargo"):
        commands.append(("cargo fmt", ["cargo", "fmt", "--all"]))
    return commands


def _lint_fix_commands(root: Path) -> List[Tuple[str, List[str]]]:
    """Return the lint autofix commands that apply to this repository."""
    commands: List[Tuple[str, List[str]]] = []
    if (root / "pyproject.toml").is_file() or any(root.rglob("*.py")):
        if which("ruff"):
            commands.append(
                ("ruff --fix", ["ruff", "check", "--fix", "--quiet", "."])
            )
    if (root / "package.json").is_file() and which("npx"):
        commands.append(
            (
                "eslint --fix",
                ["npx", "--yes", "eslint", ".", "--fix", "--quiet"],
            )
        )
    return commands


def apply_formatting(ws: GitWorkspace, settings: RepoSettings, outcome: RepoOutcome) -> None:
    """Run formatters and lint autofixes, recording the files they changed."""
    if not bool(settings.quality.get("format_on_pr", True)):
        return
    before = set(ws.tracked_files())

    if bool(settings.quality.get("auto_fix_lint", True)):
        for label, cmd in _format_commands(ws.path) + _lint_fix_commands(ws.path):
            if ws.dry_run:
                outcome.messages.append("[dry-run] would run `{0}`".format(" ".join(cmd)))
                continue
            code, out, err = run(cmd, cwd=ws.path, timeout=DEFAULT_TEST_TIMEOUT)
            if code != 0:
                detail = truncate(err or out, 160)
                outcome.messages.append(
                    "`{0}` exited {1}{2}".format(
                        label, code, ": {0}".format(detail) if detail else ""
                    )
                )
                continue
            outcome.messages.append("ran `{0}`".format(label))

    after = set(ws.tracked_files())
    touched = sorted((after - before)) or ws.changed_files()
    if touched:
        outcome.changes.append(
            Change(
                category="format",
                path=", ".join(touched[:8]) + ("..." if len(touched) > 8 else ""),
                summary="Formatted and auto-fixed {0} file(s)".format(len(touched)),
                detail="Applied repository formatters and lint autofixes.",
            )
        )


def detected_ecosystems(root: Path) -> set:
    """Return the dependency ecosystems implied by the manifests in ``root``."""
    ecosystems = set()
    for manifest in detect_manifests(root):
        if manifest.name == "package.json":
            ecosystems.add("npm")
        elif manifest.name in ("requirements.txt", "pyproject.toml", "Pipfile", "poetry.lock"):
            ecosystems.add("python")
    return ecosystems


def apply_deprecation_repairs(ws: GitWorkspace, outcome: RepoOutcome) -> None:
    """Apply safe, purely textual deprecation repairs to source files."""
    ecosystems = detected_ecosystems(ws.path)
    editable = {".py", ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"}
    repaired: List[str] = []
    for path in iter_repo_files(ws.path, limit=3000):
        if path.suffix.lower() not in editable:
            continue
        if ws.dry_run:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        updated = text
        for old, new, applies_to in DEPRECATION_REPAIRS:
            if ecosystems and not (ecosystems & set(applies_to)):
                continue
            if old in updated:
                updated = updated.replace(old, new)
        if updated != text:
            try:
                path.write_text(updated, encoding="utf-8")
                repaired.append(str(path.relative_to(ws.path)))
            except OSError:
                continue

    if repaired:
        outcome.changes.append(
            Change(
                category="deprecation",
                path=", ".join(repaired[:8]),
                summary="Replaced {0} deprecated API usage(s)".format(len(repaired)),
                detail="Swapped removed stdlib and legacy JS APIs for supported equivalents.",
            )
        )
    elif ws.dry_run:
        outcome.messages.append("[dry-run] would scan for deprecated API usage")


def _bump_allowed_kinds(settings: RepoSettings) -> List[str]:
    """Return the semver bump kinds Phase 2 is permitted to apply."""
    allowed: List[str] = []
    if bool(settings.dependencies.get("auto_bump_patch", True)):
        allowed.append("patch")
    if bool(settings.dependencies.get("allow_minor_bumps", False)):
        allowed.append("minor")
    if bool(settings.dependencies.get("allow_major_bumps", False)):
        allowed.append("major")
    return allowed


def apply_patch_bumps(ws: GitWorkspace, settings: RepoSettings, outcome: RepoOutcome) -> None:
    """Bump semver-safe direct dependencies, honouring the configured limits."""
    if not bool(settings.dependencies.get("enabled", True)):
        return
    allowed = _bump_allowed_kinds(settings)
    if not allowed:
        outcome.messages.append(
            "dependency bumping disabled (no bump kind is enabled in config)"
        )
        return
    if "npm" not in detected_ecosystems(ws.path):
        return
    if ws.dry_run:
        outcome.messages.append("[dry-run] would evaluate semver-safe dependency bumps")
        return

    outdated = _npm_outdated(ws)
    eligible = [
        item
        for item in outdated
        if item.get("kind") in allowed and item.get("direct") == "true"
    ][: settings.max_bumps]
    if not eligible:
        if outdated:
            outcome.messages.append(
                "no dependency bump was safe: {0} update(s) found, none within the allowed "
                "kinds ({1})".format(len(outdated), ", ".join(allowed))
            )
        return

    manifest = ws.path / "package.json"
    try:
        payload = json.loads(read_text_safe(manifest) or "{}")
    except ValueError:
        outcome.messages.append("package.json is not valid JSON; skipping bumps")
        return
    if not isinstance(payload, dict):
        return

    bumped: List[str] = []
    for item in eligible:
        name, target = item["name"], item["latest"]
        for section in ("dependencies", "devDependencies"):
            block = payload.get(section)
            if isinstance(block, dict) and name in block:
                block[name] = target
                bumped.append("{0} {1} -> {2}".format(name, item["current"], target))
                break
    if not bumped:
        return

    # Preserve key order and use the conventional 2-space indentation.
    try:
        manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        outcome.messages.append("could not write package.json: {0}".format(exc))
        return

    outcome.changes.append(
        Change(
            category="dependencies",
            path="package.json",
            summary="Bumped {0} dependency version(s) ({1})".format(
                len(bumped), ", ".join(allowed)
            ),
            detail="; ".join(bumped[:10]),
        )
    )


LICENSE_BADGE_TEMPLATE = (
    "[![License: {0}](https://img.shields.io/badge/License-{0}-blue.svg)]"
    "({1}/blob/{2}/LICENSE)"
)


def _detect_license(root: Path) -> Optional[str]:
    """Return the name of a license file in ``root``, if one exists."""
    try:
        entries = sorted(path for path in root.iterdir() if path.is_file())
    except OSError:
        return None
    for path in entries:
        if re.match(r"^(licen[cs]e|copying)(\..*)?$", path.name, re.IGNORECASE):
            return path.name
    return None


def update_readme_metadata(ws: GitWorkspace, settings: RepoSettings, outcome: RepoOutcome) -> None:
    """Add a missing license badge and normalise README line endings."""
    root = ws.path
    readme = find_first(root, ["README.md", "README.rst", "README", "readme.md"])
    if readme is None:
        return
    text = read_text_safe(readme)
    if not text:
        return

    original = text
    default_branch = ws.default_branch or "main"

    # 1) Normalise CRLF endings: a purely mechanical, always-safe repair.
    if "\r\n" in text:
        text = text.replace("\r\n", "\n")
    # 2) Ensure exactly one trailing newline.
    text = text.rstrip("\n") + "\n"

    # 3) Add a license badge when a license file exists but no badge does.
    if not re.search(r"img\.shields\.io/badge/license", text, re.IGNORECASE):
        license_file = _detect_license(root)
        if license_file:
            label = "MIT" if "mit" in license_file.lower() else license_file.split(".")[0].upper()
            badge = LICENSE_BADGE_TEMPLATE.format(
                label.replace(" ", "%20"), settings.name, default_branch
            )
            lines = text.splitlines()
            insert_at = 0
            for index, line in enumerate(lines[:6]):
                if line.startswith("# "):
                    insert_at = index + 1
                    break
            if insert_at < len(lines) and lines[insert_at].strip():
                insert_at += 1
            lines.insert(insert_at, "")
            lines.insert(insert_at + 1, badge)
            text = "\n".join(lines)
            outcome.changes.append(
                Change(
                    category="docs",
                    path=str(readme.relative_to(root)),
                    summary="Added a license badge to the README",
                    detail="Found {0} but no license badge.".format(license_file),
                )
            )

    if ws.dry_run:
        if text != original:
            outcome.messages.append("[dry-run] would normalise README formatting")
        return
    if text == original:
        return

    try:
        readme.write_text(text, encoding="utf-8")
    except OSError as exc:
        outcome.messages.append("could not write README: {0}".format(exc))
        return

    outcome.changes.append(
        Change(
            category="docs",
            path=str(readme.relative_to(root)),
            summary="Normalised README line endings and trailing newline",
            detail="Converted CRLF to LF and ensured a single trailing newline.",
        )
    )


def run_tests(ws: GitWorkspace, settings: RepoSettings, outcome: RepoOutcome) -> bool:
    """Run the repository's test suite; returns ``False`` on a hard failure."""
    if not bool(settings.quality.get("run_tests_on_pr", True)):
        outcome.tests = {"ran": False, "reason": "disabled in config"}
        return True

    detected = detect_test_command(ws.path)
    if detected is None:
        outcome.tests = {"ran": False, "reason": "no test command detected"}
        outcome.messages.append("no test command detected; skipping local validation")
        return True

    label, cmd = detected
    timeout = settings.test_timeout
    if ws.dry_run:
        outcome.tests = {"ran": False, "command": label, "reason": "dry-run"}
        outcome.messages.append("[dry-run] would run `{0}`".format(label))
        return True

    code, out, err = run(cmd, cwd=ws.path, timeout=timeout)
    passed = code == 0
    outcome.tests = {
        "ran": True,
        "command": label,
        "passed": passed,
        "exit_code": code,
        "timeout_seconds": timeout,
        "output_tail": truncate((out or err), 2000),
    }
    outcome.messages.append(
        "`{0}` {1} (exit {2})".format(label, "passed" if passed else "FAILED", code)
    )
    return passed


def render_pr_body(outcome: RepoOutcome, settings: RepoSettings, *, base: str) -> str:
    """Render the PR body: executive summary table, changelog, and safety notes."""
    tests = outcome.tests or {}
    if tests.get("ran"):
        test_status = "passed" if tests.get("passed") else "FAILED"
        test_detail = "`{0}` (exit {1})".format(tests.get("command"), tests.get("exit_code"))
    else:
        test_status = "not run"
        test_detail = str(tests.get("reason") or "no test suite detected")

    lines: List[str] = [
        "## Automated daily maintenance",
        "",
        "Opened by `repo_maintainer.py` (Phase 2). This pull request contains only "
        "mechanical, non-destructive upkeep. No behavioural changes are intended.",
        "",
        "### Executive summary",
        "",
        "| Item | Value |",
        "| --- | --- |",
        "| Base branch | `{0}` |".format(base),
        "| Branch | `{0}` |".format(outcome.branch or "(dry-run)"),
        "| Change categories | {0} |".format(
            ", ".join(sorted({change.category for change in outcome.changes})) or "none"
        ),
        "| Files changed | {0} |".format(len({change.path for change in outcome.changes})),
        "| Local validation | {0} |".format(test_detail),
        "| Validation result | **{0}** |".format(test_status),
        "",
        "### Changelog",
        "",
    ]

    if not outcome.changes:
        lines.append("_No changes were produced._")
    else:
        grouped: Dict[str, List[Change]] = {}
        for change in outcome.changes:
            grouped.setdefault(change.category, []).append(change)
        for category in sorted(grouped):
            lines.append("**{0}**".format(category))
            lines.append("")
            for change in grouped[category]:
                lines.append("- {0}".format(change.summary))
                if change.path:
                    lines.append("  - Path: `{0}`".format(change.path))
                if change.detail:
                    lines.append("  - {0}".format(change.detail))
            lines.append("")

    lines.extend(
        [
            "### Safety and review notes",
            "",
            "- Changes were produced on a dedicated branch; the default branch was never pushed to.",
            "- Formatting and lint autofixes are safe by construction; review the deprecation edits closely.",
            "- Dependency bumps are limited to `{0}` by configuration.".format(
                ", ".join(_bump_allowed_kinds(settings)) or "no bump kinds"
            ),
            "- If `Local validation` is **FAILED**, do not merge without a manual test run.",
            "",
        ]
    )
    return "\n".join(lines)


def render_pr_body_with_diff(
    outcome: RepoOutcome,
    settings: RepoSettings,
    ws: GitWorkspace,
    *,
    base: str,
    audit: Optional[RepoAudit] = None,
) -> str:
    """Render the PR body and append the real per-file diffstat table."""
    body = render_pr_body(outcome, settings, base=base)
    if audit is not None:
        counts = audit.severity_counts()
        context = "\n".join(
            [
                "",
                "### Audit context (Phase 1)",
                "",
                "This maintenance run was triggered by an audit that scored the repository "
                "**{0}/100 (grade {1})** with {2} critical, {3} warning, and {4} informational "
                "findings.".format(
                    audit.health_score(),
                    audit.grade(),
                    counts[SEVERITY_CRITICAL],
                    counts[SEVERITY_WARNING],
                    counts[SEVERITY_INFO],
                ),
            ]
        )
        body = body + context

    rows = ws.diff_summary()
    if not rows:
        return body
    table = [
        "### Diffstat",
        "",
        "| File | Additions | Deletions |",
        "| --- | --- | --- |",
    ]
    table.extend(rows[:40])
    if len(rows) > 40:
        table.append("")
        table.append("_...and {0} more file(s)._".format(len(rows) - 40))
    return body + "\n" + "\n".join(table) + "\n"


# --------------------------------------------------------------------------- #
# Phase 4: curation pull-request body
# --------------------------------------------------------------------------- #


def render_curation_pr_body(
    outcome: RepoOutcome,
    settings: RepoSettings,
    ws: GitWorkspace,
    *,
    base: str,
) -> str:
    """Render the daily-curation pull request body.

    The body is a changelog a human can triage without opening the diff: what
    the recipe was asked to do, what preflight confirmed, exactly what was
    produced, and which post-conditions were re-checked before the branch was
    allowed to exist.
    """
    summary = outcome.curation or {}
    recipe = str(summary.get("recipe") or "curation")
    lines: List[str] = [
        "## Daily curation ({0})".format(today_iso()),
        "",
        "Autonomous expansion produced by `repo_maintainer.py --mode curate`.",
        "",
        "| | |",
        "| --- | --- |",
        "| Recipe | `{0}` |".format(recipe),
        "| Base | `{0}` |".format(base),
        "| Branch | `{0}` |".format(outcome.branch or "(none)"),
        "| Generated | {0} |".format(utcnow().isoformat()),
    ]
    if summary.get("summary"):
        lines.append("")
        lines.append(str(summary["summary"]))

    checks = list(summary.get("check") or [])
    if checks:
        lines.extend(["", "### Preflight", "", "| Check | Result | Detail |", "| --- | --- | --- |"])
        for item in checks:
            lines.append(
                "| {0} | {1} | {2} |".format(
                    item.get("name", "?"),
                    "pass"
                    if item.get("ok")
                    else ("fail" if item.get("fatal", True) else "n/a"),
                    str(item.get("detail", "")).replace("|", "\\|") or "-",
                )
            )

    items = list(summary.get("items") or [])
    lines.extend(["", "### What changed", ""])
    if items:
        by_kind: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            by_kind.setdefault(str(item.get("kind") or "item"), []).append(item)
        for kind in sorted(by_kind):
            lines.append("**{0}** ({1})".format(kind, len(by_kind[kind])))
            lines.append("")
            for item in by_kind[kind][:40]:
                location = str(item.get("path") or "")
                target = " (`{0}`)".format(location) if location else ""
                detail = str(item.get("detail") or "").replace("\n", " ").strip()
                lines.append(
                    "- {0}{1}{2}".format(item.get("title", ""), target, " - " + detail if detail else "")
                )
            if len(by_kind[kind]) > 40:
                lines.append("- _...and {0} more._".format(len(by_kind[kind]) - 40))
            lines.append("")
    else:
        lines.append("_The recipe produced no new items for this repository._")
        lines.append("")

    notes = list(summary.get("notes") or [])
    if notes:
        lines.extend(["### Run notes", ""])
        lines.extend("- {0}".format(str(note).replace("\n", " ")) for note in notes)
        lines.append("")

    verification = list(summary.get("verify") or [])
    lines.extend(["### Verification", ""])
    if verification:
        lines.extend("- {0}".format(str(item)) for item in verification)
    else:
        lines.append("Post-conditions re-checked and clean.")
    lines.append("")

    evaluation = summary.get("evaluation")
    if evaluation:
        verdict_val = evaluation.get("verdict", "APPROVED")
        score_val = evaluation.get("quality_score", 8)
        model_val = evaluation.get("evaluator_model", "jev/gemini-flash")
        is_fp = evaluation.get("is_false_positive", False)
        reason_val = evaluation.get("reason", "")
        lines.extend([
            "### Quality Gate & Evaluation (Jev / Gemini Flash)",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            "| Verdict | `{0}` |".format(verdict_val),
            "| Quality Score | `{0} / 10` |".format(score_val),
            "| Anti-False-Positive Check | `{0}` |".format("PASSED" if not is_fp else "FLAGGED"),
            "| Evaluator Model | `{0}` |".format(model_val),
            "",
            "> **Evaluator Assessment**: {0}".format(reason_val or "Verified substantive work"),
            "",
        ])

    self_correction = summary.get("self_correction")
    if self_correction and self_correction.get("total_attempts"):
        lines.extend([
            "### Self-Correction & Autonomous Recovery",
            "",
            "This run encountered issues and autonomously healed itself across `{0}` attempt(s):".format(
                self_correction.get("total_attempts")
            ),
            "",
        ])
        for att in self_correction.get("attempts", []):
            lines.append("- **Attempt {0} ({1})**: {2}".format(
                att.get("attempt"), att.get("failure_type"), att.get("root_cause") or att.get("error_message")
            ))
            if att.get("files_patched"):
                lines.append("  - Patched files: `{0}`".format(", ".join(att.get("files_patched"))))
            if att.get("packages_installed"):
                lines.append("  - Auto-installed packages: `{0}`".format(", ".join(att.get("packages_installed"))))
        lines.append("")

    rows = ws.diff_summary()
    if rows:
        lines.extend(["### Diffstat", "", "| File | Additions | Deletions |", "| --- | --- | --- |"])
        lines.extend(rows[:40])
        if len(rows) > 40:
            lines.extend(["", "_...and {0} more file(s)._".format(len(rows) - 40)])
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            "Generated by the `{0}` curator recipe via `repo_maintainer.py`. "
            "Review the preflight and verification sections above before merging; "
            "the default branch is never pushed to by this bot.".format(recipe),
            "",
        ]
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Phase 3: feature proposals and dynamic skills
# --------------------------------------------------------------------------- #


def propose_features(audit: RepoAudit, settings: RepoSettings) -> List[Dict[str, str]]:
    """Derive concrete feature and architecture proposals from audit findings."""
    proposals: List[Dict[str, str]] = []

    def add(slug: str, title: str, rationale: str, scope: str, risk: str) -> None:
        proposals.append(
            {
                "slug": slugify(slug),
                "title": title,
                "rationale": rationale,
                "scope": scope,
                "risk": risk,
            }
        )

    for finding in audit.findings:
        if finding.severity not in (SEVERITY_CRITICAL, SEVERITY_WARNING):
            continue
        if finding.category == "tests-ci" and "No GitHub Actions" in finding.title:
            add(
                "add-ci-pipeline",
                "Add a continuous integration pipeline",
                "No GitHub Actions workflow exists, so regressions are only caught by "
                "reviewers. A minimal pipeline removes an entire class of defects.",
                "Add .github/workflows/ci.yml that installs dependencies, runs the "
                "detected test command, and uploads coverage.",
                "Low: additive, and the pipeline is a no-op until it is merged.",
            )
        elif finding.category == "tests-ci" and "No test suite" in finding.title:
            add(
                "introduce-test-harness",
                "Introduce a baseline test harness",
                "The project ships no automated tests, which blocks safe refactoring "
                "and makes every maintenance change riskier than it needs to be.",
                "Add a minimal test directory, a runner configuration, and three "
                "smoke tests covering the public entry points.",
                "Low: purely additive.",
            )
        elif finding.category == "docs" and "Broken relative links" in finding.title:
            add(
                "docs-link-integrity-check",
                "Add a documentation link checker to CI",
                "The documentation contains broken internal links, and nothing "
                "prevents new ones from appearing.",
                "Add a CI step that validates relative Markdown links and fails the "
                "build when a target disappears.",
                "Low: CI-only change with no runtime impact.",
            )
        elif finding.category == "dependencies" and "major version" in finding.title:
            add(
                "dependency-upgrade-plan",
                "Plan a major dependency upgrade",
                "Direct dependencies are behind by a major version, which usually "
                "carries deprecations and breaking changes.",
                "Write a migration note covering each breaking change, upgrade one "
                "dependency per pull request, and keep a rollback path.",
                "Medium: breaking changes require code updates and retesting.",
            )
        elif finding.category == "staleness" and "Repository is stale" in finding.title:
            add(
                "deprecation-notice",
                "Publish a maintenance-status notice",
                "The project has had no commits for an extended period, so users "
                "cannot tell whether it is abandoned or merely quiet.",
                "Add a short status section to the README stating the maintenance "
                "posture and the conditions for archiving.",
                "Low: documentation only.",
            )
        elif finding.category == "docs" and "No status badges" in finding.title:
            add(
                "add-status-badges",
                "Surface build and coverage status in the README",
                "Without badges, visitors have no quick signal about project health.",
                "Add CI, coverage, and license badges at the top of the README.",
                "Low: documentation only.",
            )

    if not proposals:
        proposals.append(
            {
                "slug": "no-action-required",
                "title": "No corrective feature work required",
                "rationale": "The audit surfaced no critical or warning-level findings "
                "that map to a feature-level improvement.",
                "scope": "No changes proposed. Continue routine maintenance.",
                "risk": "None.",
            }
        )
    return proposals


def render_proposal_document(audit: RepoAudit, proposal: Dict[str, str]) -> str:
    """Render a single feature proposal as a standalone Markdown document."""
    return "\n".join(
        [
            "# Proposal: {0}".format(proposal["title"]),
            "",
            "| Field | Value |",
            "| --- | --- |",
            "| Repository | `{0}` |".format(audit.url),
            "| Branch | `{0}` |".format(FEATURE_BRANCH_TEMPLATE.format(slug=proposal["slug"])),
            "| Health score at time of proposal | {0}/100 (grade {1}) |".format(
                audit.health_score(), audit.grade()
            ),
            "",
            "## Rationale",
            "",
            proposal["rationale"],
            "",
            "## Proposed scope",
            "",
            proposal["scope"],
            "",
            "## Risk assessment",
            "",
            proposal["risk"],
            "",
            "## Alternatives considered",
            "",
            "- Doing nothing: the underlying issue persists and is rediscovered by "
            "each future audit.",
            "- A larger refactor: higher risk and slower to review than the scoped "
            "change above.",
            "",
            "## How to validate",
            "",
            "1. Apply the change on a branch.",
            "2. Run the project's test suite and confirm it passes.",
            "3. Re-run `repo_maintainer.py --mode observer` and confirm the related "
            "finding is resolved.",
            "",
        ]
    )


def render_proposal_index(audit: RepoAudit, proposals: Sequence[Dict[str, str]]) -> str:
    """Render the index document that lists every proposal for a repository."""
    lines = [
        "# Feature and Architecture Proposals: {0}".format(audit.name),
        "",
        "> Generated by `repo_maintainer.py` in Phase 3 on {0}.".format(today_iso()),
        "",
        "The following proposals were derived automatically from the Phase 1 audit. "
        "Each one lives in its own branch and pull request so it can be reviewed "
        "independently.",
        "",
        "| # | Proposal | Branch | Risk |",
        "| --- | --- | --- | --- |",
    ]
    for index, proposal in enumerate(proposals, 1):
        lines.append(
            "| {0} | [{1}]({2}.md) | `{3}` | {4} |".format(
                index,
                proposal["title"],
                proposal["slug"],
                FEATURE_BRANCH_TEMPLATE.format(slug=proposal["slug"]),
                proposal["risk"].split(":")[0],
            )
        )
    lines.extend(
        [
            "",
            "## Current health snapshot",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            "| Health score | {0}/100 (grade {1}) |".format(audit.health_score(), audit.grade()),
            "| Critical findings | {0} |".format(audit.severity_counts()[SEVERITY_CRITICAL]),
            "| Warning findings | {0} |".format(audit.severity_counts()[SEVERITY_WARNING]),
            "",
        ]
    )
    return "\n".join(lines)


def skill_domains_from_audit(audit: RepoAudit) -> List[str]:
    """Infer tooling domains for which a skill may be missing.

    Domains are derived from concrete signals in the audit (package managers,
    formatters, CI systems) rather than guessed, so a generated skill always
    corresponds to tooling the repository actually uses.
    """
    domains: List[str] = []

    def push(value: str) -> None:
        if value and value not in domains:
            domains.append(value)

    for status in audit.dependencies:
        if status.ecosystem == "npm":
            push("npm dependency management")
        elif status.ecosystem == "python":
            push("python packaging")

    if any(workflow.endswith((".yml", ".yaml")) for workflow in audit.test_summary.get("ci_workflow_names", [])):
        push("github actions")
    if audit.test_summary.get("test_command"):
        command = str(audit.test_summary.get("test_command"))
        if "pytest" in command:
            push("pytest")
        elif "jest" in command or "vitest" in command or "npm test" in command:
            push("javascript testing")
        elif "go test" in command:
            push("go testing")
    if audit.docs_summary.get("broken_links"):
        push("markdown link checking")
    if audit.language:
        push("{0} project tooling".format(audit.language.lower()))
    return domains


def ensure_skill_coverage(
    domains: Sequence[str], settings: RepoSettings, outcome: RepoOutcome, *, dry_run: bool
) -> None:
    """Ensure a validated skill exists for each uncovered tooling domain.

    Discovery, staging, and generation are all delegated to
    :class:`skill_manager.SkillManager`, which confines every write to a
    sandboxed staging directory.
    """
    skills_cfg = settings.skills
    manager = skill_manager.SkillManager(dry_run=dry_run)
    allow_search = bool(skills_cfg.get("search", True))
    allow_generate = bool(skills_cfg.get("generate", True))
    limit = _as_int(skills_cfg.get("max_search_results", 5), 5)

    outcome.messages.append(
        "skill staging directory: {0}{1}".format(
            manager.staging_dir, " (dry-run)" if dry_run else ""
        )
    )

    if allow_search:
        available, detail = manager.search_available()
        outcome.messages.append("skill search: {0}".format(detail))
        if not available:
            allow_search = False

    for domain in domains:
        try:
            if manager.has_coverage(domain):
                outcome.skills.append({"domain": domain, "action": "already-covered"})
                continue
            if allow_search:
                results = manager.search(domain, limit=limit)
                if results:
                    record = manager.stage_discovered(results[0])
                    outcome.skills.append(
                        {
                            "domain": domain,
                            "action": "discovered",
                            "name": record.name,
                            "valid": record.valid,
                            "path": str(record.path) if record.path else None,
                            "source": record.source_url,
                        }
                    )
                    continue
            if allow_generate:
                record = manager.generate_skill(
                    domain=domain,
                    summary="Guidance for maintaining {0} in the {1} repository, "
                    "including the commands this project actually uses.".format(
                        domain, settings.name
                    ),
                    commands=_skill_commands_for(settings, audit),
                )
                outcome.skills.append(
                    {
                        "domain": domain,
                        "action": "generated",
                        "name": record.name,
                        "valid": record.valid,
                        "path": str(record.path) if record.path else None,
                        "errors": list(record.errors),
                    }
                )
                continue
            outcome.skills.append({"domain": domain, "action": "skipped"})
        except Exception as exc:  # never let skill tooling abort a phase
            debug("skill handling failed for '{0}': {1}".format(domain, exc))
            outcome.skills.append(
                {"domain": domain, "action": "error", "error": truncate(str(exc), 200)}
            )


def _skill_commands_for(settings: RepoSettings, audit: RepoAudit) -> List[str]:
    """Return reference commands embedded in a generated skill.

    Commands are chosen from the tooling the audit actually observed, so a
    generated skill never advertises a tool the repository does not use.
    """
    commands = [
        "python3 repo_maintainer.py --check-only",
        "python3 repo_maintainer.py --mode observer --repo {0}".format(settings.name),
    ]
    ecosystems = {status.ecosystem for status in audit.dependencies}
    if "npm" in ecosystems:
        commands.append("npm outdated")
    if "python" in ecosystems:
        commands.append("pip list --outdated")
    if audit.test_summary.get("ci_workflows"):
        commands.append("gh workflow list --repo {0}".format(settings.name))
    test_command = audit.test_summary.get("test_command")
    if test_command:
        commands.append(str(test_command))
    return commands


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


class Maintainer:
    """Runs a selected phase across the configured repositories."""

    def __init__(
        self,
        config: Dict[str, Any],
        *,
        config_path: Optional[Path] = None,
        workspace_root: Optional[Path] = None,
        reports_root: Optional[Path] = None,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.config_path = Path(config_path or DEFAULT_CONFIG_PATH)
        self.workspace_root = Path(workspace_root or (ROOT_DIR / WORKSPACE_DIRNAME))
        self.reports_root = Path(reports_root or (ROOT_DIR / REPORTS_DIRNAME))
        self.dry_run = bool(dry_run)
        self._temp_workspace: Optional[tempfile.TemporaryDirectory] = None

    # -- workspace lifecycle ----------------------------------------------- #

    def _active_workspace_root(self) -> Path:
        """Return the workspace root, using a temp dir during dry-runs.

        A dry-run must leave no trace on disk, so it clones into a temporary
        directory that is discarded as soon as the phase finishes.
        """
        if not self.dry_run:
            return self.workspace_root
        if self._temp_workspace is None:
            self._temp_workspace = tempfile.TemporaryDirectory(
                prefix="repo-maintainer-dryrun-"
            )
        return Path(self._temp_workspace.name)

    def cleanup(self) -> None:
        """Discard any temporary workspace created for a dry-run."""
        if self._temp_workspace is not None:
            self._temp_workspace.cleanup()
            self._temp_workspace = None

    # -- audit (shared by every phase) -------------------------------------- #

    def audit_repository(self, settings: RepoSettings) -> RepoAudit:
        """Clone/fetch a repository and run every Phase 1 check against it."""
        ws = GitWorkspace(settings, self._active_workspace_root(), dry_run=self.dry_run)
        ws.clone()
        if not ws.exists and not self.dry_run:
            raise MaintenanceError("workspace was not created for {0}".format(settings.name))

        default_branch = ws.detect_default_branch()
        audit = RepoAudit(
            name=settings.name,
            url=settings.url,
            slug=settings.slug,
            path=ws.path,
            default_branch=default_branch,
        )
        audit.head_sha = ws.head_sha()
        audit.language = ws.primary_language()

        check_staleness(audit, ws, settings)
        check_documentation(audit, ws, settings)
        check_dependencies(audit, ws, settings)
        check_tests_and_ci(audit, ws, settings)
        return audit

    def _workspace_for(
        self, settings: RepoSettings, *, materialise: bool = False
    ) -> GitWorkspace:
        """Return a workspace already primed on the default branch.

        ``materialise=True`` forces a real clone even under ``--dry-run``, which
        Phase 4 needs: a curation preview has to execute the recipe to be worth
        previewing. It is only safe because ``_active_workspace_root()`` points
        a dry-run at a temporary directory that is deleted afterwards.
        """
        ws = GitWorkspace(
            settings,
            self._active_workspace_root(),
            dry_run=self.dry_run,
            materialise=materialise,
        )
        ws.clone()
        ws.detect_default_branch()
        return ws

    def _write_report(self, audit: RepoAudit, settings: RepoSettings) -> Optional[Path]:
        """Render and persist the Phase 1 health report."""
        report = render_health_report(audit, settings, generated_at=today_iso())
        name = "health_report_{0}_{1}.md".format(audit.slug, today_iso())
        target = self.reports_root / name
        if self.dry_run:
            _log("[dry-run] would write report to {0}".format(relative_to_cwd(target)))
            return None
        self.reports_root.mkdir(parents=True, exist_ok=True)
        target.write_text(report, encoding="utf-8")
        _log("wrote report {0}".format(relative_to_cwd(target)))
        return target

    # -- Phase 1 ------------------------------------------------------------- #

    def run_observer(self, settings: RepoSettings) -> RepoOutcome:
        """Run the read-only health and staleness audit for one repository."""
        outcome = RepoOutcome(repo=settings.name, mode=MODE_OBSERVER)
        try:
            audit = self.audit_repository(settings)
            outcome.report_path = self._write_report(audit, settings)
            counts = audit.severity_counts()
            outcome.status = "ok"
            outcome.messages.append(
                "health {0}/100 (grade {1}): {2} critical, {3} warning, {4} info, {5} ok".format(
                    audit.health_score(),
                    audit.grade(),
                    counts[SEVERITY_CRITICAL],
                    counts[SEVERITY_WARNING],
                    counts[SEVERITY_INFO],
                    counts[SEVERITY_OK],
                )
            )
        except (MaintenanceError, OSError) as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
        return outcome


    # -- Phase 2 ------------------------------------------------------------- #

    def run_pr(self, settings: RepoSettings) -> RepoOutcome:
        """Run guarded automated maintenance and open a pull request."""
        outcome = RepoOutcome(repo=settings.name, mode=MODE_PR)
        try:
            ws = self._workspace_for(settings)
            audit = self.audit_repository(settings)

            branch = MAINTENANCE_BRANCH_TEMPLATE.format(date=today_iso())
            ws.create_branch(branch)
            outcome.branch = branch

            apply_formatting(ws, settings, outcome)
            apply_deprecation_repairs(ws, outcome)
            apply_patch_bumps(ws, settings, outcome)
            update_readme_metadata(ws, settings, outcome)

            if not outcome.changes and not ws.has_changes():
                outcome.status = "no-changes"
                outcome.messages.append("nothing to change; no pull request was opened")
                return outcome

            if not run_tests(ws, settings, outcome):
                # A failing suite invalidates the branch: discard it entirely.
                ws.reset_hard("HEAD")
                outcome.status = "failed"
                outcome.error = "local test suite failed; branch discarded and no PR opened"
                return outcome

            message = "\n".join(
                [
                    "chore: automated daily maintenance ({0})".format(today_iso()),
                    "",
                    "Non-destructive upkeep applied by repo_maintainer.py Phase 2.",
                    "",
                ]
                + ["- {0}".format(change.summary) for change in outcome.changes]
            )
            outcome.commit = ws.commit_all(message)
            if not outcome.commit:
                outcome.status = "no-changes"
                outcome.messages.append("no net changes after formatting; no PR opened")
                return outcome

            # This branch is rebuilt from origin/<default> each run, so a
            # same-day re-run rewrites it; --force-with-lease still refuses if
            # a human has pushed to it in the meantime.
            ws.push_branch(branch, force=True)
            outcome.pr_url = self._open_pull_request(ws, settings, outcome, audit)
            if outcome.pr_url is None:
                outcome.status = "failed"
                outcome.error = "branch pushed but the pull request could not be created"
            else:
                outcome.status = "ok"
        except (MaintenanceError, OSError) as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
        return outcome

    def _open_pull_request(
        self,
        ws: GitWorkspace,
        settings: RepoSettings,
        outcome: RepoOutcome,
        audit: RepoAudit,
    ) -> Optional[str]:
        """Create the pull request with ``gh``; returns its URL or ``None``."""
        if self.dry_run:
            outcome.messages.append(
                "[dry-run] would open a pull request for '{0}'".format(outcome.branch)
            )
            return None

        # A same-day rerun should update the existing PR rather than fail.
        code, out, _err = ws._gh("pr", "view", outcome.branch, "--json", "url")
        if code == 0 and out.strip():
            try:
                return str(json.loads(out).get("url") or "") or None
            except ValueError:
                pass

        body = render_pr_body_with_diff(
            outcome, settings, ws, base=ws.default_branch or "main", audit=audit
        )
        title = "chore: automated daily maintenance ({0})".format(today_iso())
        code, out, err = ws._gh("pr", "create", "--base", ws.default_branch, "--head",
                                outcome.branch, "--title", title, "--body", body)
        if code != 0:
            detail = truncate(err or out, 300)
            outcome.messages.append("gh pr create failed: {0}".format(detail))
            return None

        url = ""
        for line in out.splitlines():
            if line.strip().startswith("http"):
                url = line.strip()
                break
        ws.add_labels(
            [settings.labels.get("maintenance", ""), settings.labels.get("audit", "")], outcome.branch
        )
        outcome.messages.append("opened pull request: {0}".format(url or "(url unavailable)"))
        return url or "created"


    # -- Phase 3 ------------------------------------------------------------- #

    def run_feature(self, settings: RepoSettings) -> RepoOutcome:
        """Propose features on dedicated branches and expand skill coverage."""
        outcome = RepoOutcome(repo=settings.name, mode=MODE_FEATURE)
        try:
            audit = self.audit_repository(settings)
            proposals = propose_features(audit, settings)
            outcome.proposals = [proposal["slug"] for proposal in proposals]
            outcome.report_path = self._write_report(audit, settings)

            # Skill expansion is independent of the git work and runs first so
            # its results appear even when no proposal warrants a branch.
            domains = skill_domains_from_audit(audit)
            if domains:
                ensure_skill_coverage(domains, settings, outcome, dry_run=self.dry_run)
            else:
                outcome.messages.append("no uncovered tooling domains were detected")

            real = [p for p in proposals if p["slug"] != "no-action-required"]
            if not real:
                outcome.status = "no-changes"
                outcome.messages.append(
                    "audit found nothing critical; no feature branches were created"
                )
                return outcome

            for proposal in real:
                self._publish_proposal(settings, audit, proposal, outcome)
            outcome.status = "ok"
        except (MaintenanceError, OSError) as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
        return outcome

    def _publish_proposal(
        self,
        settings: RepoSettings,
        audit: RepoAudit,
        proposal: Dict[str, str],
        outcome: RepoOutcome,
    ) -> None:
        """Create a ``feature/<slug>`` branch containing one proposal document."""
        ws = self._workspace_for(settings)
        branch = FEATURE_BRANCH_TEMPLATE.format(slug=proposal["slug"])
        try:
            ws.create_branch(branch)
        except MaintenanceError as exc:
            outcome.messages.append(
                "skipped proposal '{0}': {1}".format(proposal["slug"], exc)
            )
            return

        docs_dir = ws.path / "docs" / "proposals"
        if self.dry_run:
            outcome.messages.append(
                "[dry-run] would add docs/proposals/{0}.md on '{1}' and open a PR".format(
                    proposal["slug"], branch
                )
            )
            return

        try:
            docs_dir.mkdir(parents=True, exist_ok=True)
            (docs_dir / "{0}.md".format(proposal["slug"])).write_text(
                render_proposal_document(audit, proposal), encoding="utf-8"
            )
            (docs_dir / "README.md").write_text(
                render_proposal_index(audit, [proposal]), encoding="utf-8"
            )
        except OSError as exc:
            outcome.messages.append(
                "could not write proposal files for '{0}': {1}".format(
                    proposal["slug"], exc
                )
            )
            return

        outcome.changes.append(
            Change(
                category="feature",
                path="docs/proposals/{0}.md".format(proposal["slug"]),
                summary=proposal["title"],
                detail=proposal["rationale"],
            )
        )
        commit = ws.commit_all(
            "\n".join(
                [
                    "docs: propose {0}".format(proposal["title"].lower()),
                    "",
                    proposal["rationale"],
                    "",
                    "Branch: {0}".format(branch),
                ]
            )
        )
        if not commit:
            return
        ws.push_branch(branch)
        outcome.messages.append("proposal ready on branch '{0}'".format(branch))

        code, out, err = ws._gh(
            "pr",
            "create",
            "--base",
            ws.default_branch or "main",
            "--head",
            branch,
            "--title",
            "docs: {0}".format(proposal["title"]),
            "--body",
            render_proposal_document(audit, proposal),
        )
        if code != 0:
            outcome.messages.append(
                "gh pr create failed for '{0}': {1}".format(
                    branch, truncate(err or out, 200)
                )
            )
        else:
            outcome.messages.append("opened proposal pull request for '{0}'".format(branch))
        debug("gh output: {0}".format(truncate(out, 200)))

    # -- Phase 4 ------------------------------------------------------------- #

    def build_curator(self, settings: RepoSettings, *, use_llm: bool = True):
        """Instantiate the curator recipe declared for ``settings``.

        A dry-run always builds the recipe offline: a preview must never
        spend Gemini quota or call a synthesis model over the network.
        """
        if curators is None:
            raise CurationError(
                "the curators package is unusable: {0}".format(CURATORS_IMPORT_ERROR)
            )
        recipe_id = settings.curator_recipe()
        if not recipe_id:
            raise CurationError(
                "no curator recipe is configured for {0}".format(settings.name)
            )
        options = settings.curator_options()
        if not use_llm:
            options["llm"] = False
        return curators.build_recipe(
            recipe_id,
            options,
            log=lambda message: _log("{0}: {1}".format(settings.name, message)),
            use_llm=use_llm,
        )

    def run_curate(self, settings: RepoSettings, *, use_llm: bool = True) -> RepoOutcome:
        """Run a repository's daily expansion recipe and open a pull request.

        The order is deliberate and is the safety property of this phase:
        preflight first (so a mis-clone never gets a branch), then curate on a
        throwaway branch, then ``verify`` (so a broken tree is discarded
        instead of shipped), and only then commit, push and open the PR.
        """
        outcome = RepoOutcome(repo=settings.name, mode=MODE_CURATE)
        try:
            recipe = self.build_curator(settings, use_llm=use_llm and not self.dry_run)
        except CurationError as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
            return outcome

        summary: Dict[str, Any] = {
            "recipe": recipe.recipe_id,
            "title": recipe.title,
            "summary": recipe.summary,
            "options": settings.curator_options(),
        }
        outcome.curation = summary

        try:
            # Curation always wants a real checkout, including in a dry-run:
            # the recipe has to actually run to be previewed. Under
            # --dry-run that checkout lives in a temporary directory.
            ws = self._workspace_for(settings, materialise=True)
            if not ws.exists:
                outcome.status = "failed"
                outcome.error = "could not obtain a clone of {0} to curate".format(
                    settings.url
                )
                return outcome
            outcome.branch = CURATION_BRANCH_TEMPLATE.format(date=today_iso())

            # 1) Preflight ---------------------------------------------------- #
            report = recipe.check(ws.path)
            if not report.ok:
                # Attempt automated dependency installation if missing modules caused failure
                failed_items = [item for item in report.items if item.fatal and not item.ok]
                failed_details = " ".join(item.detail or "" for item in failed_items)
                installed, install_msg = curators.auto_install_from_error(
                    failed_details, cwd=ws.path, log_fn=lambda m: _log("{0}: {1}".format(settings.name, m))
                )
                if installed:
                    outcome.messages.append("preflight self-healing: {0}".format(install_msg))
                    report = recipe.check(ws.path)

            summary["check"] = [item.to_dict() for item in report.items]
            for item in report.items:
                outcome.messages.append(
                    "preflight {0}: {1}{2}".format(
                        item.name,
                        "ok" if item.ok else ("FAILED" if item.fatal else "skipped"),
                        " ({0})".format(item.detail) if item.detail else "",
                    )
                )
            if not report.ok:
                failed = [
                    "{0}: {1}".format(item.name, item.detail or "failed")
                    for item in report.items
                    if item.fatal and not item.ok
                ]
                outcome.status = "failed"
                outcome.error = "preflight failed for {0} ({1})".format(
                    recipe.recipe_id, "; ".join(failed) or "unknown reason"
                )
                return outcome

            # Initialize self-corrector for this run
            corrector = curators.SelfCorrector(
                log=lambda m: _log("{0}: {1}".format(settings.name, m))
            )

            # 2) Curate on a guarded branch ----------------------------------- #
            ws.create_branch(outcome.branch)
            try:
                result = recipe.curate(ws.path, dry_run=self.dry_run)
            except Exception as exc:
                outcome.messages.append("curation raised exception: {0}".format(exc))
                # Attempt self-correction on exception (e.g. missing package)
                installed, install_msg = corrector.attempt_auto_install(str(exc), cwd=ws.path)
                if installed:
                    outcome.messages.append("curation dependency recovered: {0}".format(install_msg))
                    result = recipe.curate(ws.path, dry_run=self.dry_run)
                else:
                    raise

            summary["items"] = [item.to_dict() for item in result.items]
            summary["notes"] = list(result.notes)
            summary["writes"] = list(result.writes)
            summary["llm_used"] = bool(result.llm_used)
            summary["dry_run"] = bool(result.dry_run)
            for item in result.items:
                outcome.changes.append(
                    Change(
                        category=item.kind,
                        path=item.path,
                        summary=item.title,
                        detail=item.detail,
                    )
                )
            for note in result.notes:
                outcome.messages.append(str(note))

            # If recipe reports problems, attempt self-correction
            if result.problems:
                outcome.messages.append("curation reported problems; attempting self-correction...")
                repaired, repair_msg = corrector.correct_verification_problems(
                    repo_name=settings.name,
                    workspace_path=ws.path,
                    problems=result.problems,
                    candidate_files=list(result.writes),
                    attempt_idx=1,
                )
                if repaired:
                    outcome.messages.append("self-correction applied: {0}".format(repair_msg))
                    result.problems.clear()
                else:
                    if not self.dry_run:
                        ws.reset_hard("HEAD")
                    outcome.status = "failed"
                    outcome.error = "curation reported problems: {0}".format(
                        "; ".join(result.problems)[:400]
                    )
                    return outcome

            # 3) Post-conditions & Self-Correction Loop ------------------------ #
            if self.dry_run:
                summary["verify"] = [
                    "skipped during --dry-run: the working tree is intentionally unwritten"
                ]
            else:
                problems = list(recipe.verify(ws.path))
                if problems:
                    outcome.messages.append("post-condition check encountered problems; activating self-correction loop...")
                    for attempt_idx in range(1, corrector.max_attempts + 1):
                        repaired, repair_msg = corrector.correct_verification_problems(
                            repo_name=settings.name,
                            workspace_path=ws.path,
                            problems=problems,
                            candidate_files=list(result.writes),
                            attempt_idx=attempt_idx,
                        )
                        if repaired:
                            problems = list(recipe.verify(ws.path))
                            if not problems:
                                outcome.messages.append("self-correction healed verification problems ({0})".format(repair_msg))
                                break
                summary["verify"] = problems
                if problems:
                    ws.reset_hard("HEAD")
                    outcome.status = "failed"
                    outcome.error = "post-condition check failed after self-correction attempts: {0}".format(
                        "; ".join(problems)[:400]
                    )
                    return outcome
                outcome.messages.append("post-condition verification passed")

            if not result.changed and not ws.has_changes():
                outcome.status = "no-changes"
                outcome.messages.append(
                    "curation found nothing new for today; no pull request was opened"
                )
                return outcome

            # 4) Self-Evaluating & Anti-False-Positive Gate (Jev / Gemini Flash) - #
            if not self.dry_run:
                evaluator = curators.CurationEvaluator(
                    log=lambda m: _log("{0}: {1}".format(settings.name, m))
                )
                _, diff_sample, _ = ws._git("diff", "HEAD")
                verdict = evaluator.evaluate(
                    repo_name=settings.name,
                    workspace_path=ws.path,
                    files_touched=list(result.writes),
                    items_summary=[item.to_dict() for item in result.items],
                    diff_text=diff_sample or "",
                )
                summary["evaluation"] = verdict.to_dict()
                summary["self_correction"] = corrector.report.to_dict()
                outcome.messages.append(
                    "evaluation [{0}]: verdict={1}, score={2}/10, false_positive={3}".format(
                        verdict.evaluator_model, verdict.verdict, verdict.quality_score, verdict.is_false_positive
                    )
                )

                if not verdict.passed:
                    outcome.messages.append("evaluation rejected run; triggering generative enrichment...")
                    enriched, enrich_msg = corrector.correct_evaluation_rejection(
                        repo_name=settings.name,
                        workspace_path=ws.path,
                        verdict=verdict,
                        candidate_files=list(result.writes),
                        attempt_idx=len(corrector.report.attempts) + 1,
                    )
                    if enriched:
                        problems = list(recipe.verify(ws.path))
                        if not problems:
                            _, diff_sample, _ = ws._git("diff", "HEAD")
                            verdict = evaluator.evaluate(
                                repo_name=settings.name,
                                workspace_path=ws.path,
                                files_touched=list(result.writes),
                                items_summary=[item.to_dict() for item in result.items],
                                diff_text=diff_sample or "",
                            )
                            summary["evaluation"] = verdict.to_dict()
                            summary["self_correction"] = corrector.report.to_dict()

                if not verdict.passed:
                    ws.reset_hard("HEAD")
                    outcome.status = "failed"
                    outcome.error = "evaluation rejected work as false-positive or low quality (score {0}/10, model {1}): {2}".format(
                        verdict.quality_score, verdict.evaluator_model, verdict.reason
                    )
                    return outcome

            # 4) Publish -------------------------------------------------------- #
            if self.dry_run:
                outcome.status = "preview"
                outcome.messages.append(
                    "[dry-run] would commit {0} file(s) on '{1}', push it, and open "
                    "a pull request against {2}".format(
                        len(result.writes), outcome.branch, ws.default_branch or "main"
                    )
                )
                return outcome

            message = "\n".join(
                [
                    "chore: daily curation ({0})".format(today_iso()),
                    "",
                    "Recipe: {0} - {1}".format(recipe.recipe_id, recipe.title),
                    "",
                    "Produced {0} item(s) across {1} file(s).".format(
                        len(result.items), len(result.writes)
                    ),
                    "",
                ]
                + [
                    "- {0}{1}".format(
                        item.title, " ({0})".format(item.path) if item.path else ""
                    )
                    for item in result.items[:20]
                ]
            )
            outcome.commit = ws.commit_all(message)
            if not outcome.commit:
                outcome.status = "no-changes"
                outcome.messages.append(
                    "curation produced no net change after verification; no PR opened"
                )
                return outcome

            ws.push_branch(outcome.branch, force=True)
            outcome.pr_url = self._open_curation_pull_request(ws, settings, outcome)
            outcome.status = "ok" if outcome.pr_url else "failed"
            if not outcome.pr_url:
                outcome.error = "branch pushed but the pull request could not be created"
        except (MaintenanceError, CurationError, OSError) as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
        return outcome

    def _open_curation_pull_request(
        self, ws: GitWorkspace, settings: RepoSettings, outcome: RepoOutcome
    ) -> Optional[str]:
        """Create the daily-curation pull request; returns its URL or ``None``."""
        if self.dry_run:
            outcome.messages.append(
                "[dry-run] would open a curation pull request for '{0}'".format(
                    outcome.branch
                )
            )
            return None

        branch = outcome.branch or ""
        base = ws.default_branch or "main"
        # A same-day rerun should update the existing PR rather than fail.
        code, out, _err = ws._gh("pr", "view", branch, "--json", "url")
        if code == 0 and out.strip():
            try:
                existing = str(json.loads(out).get("url") or "")
            except ValueError:
                existing = ""
            if existing:
                outcome.messages.append(
                    "a pull request already exists for '{0}': {1}".format(branch, existing)
                )
                return existing

        recipe = str((outcome.curation or {}).get("recipe") or "curation")
        body = render_curation_pr_body(outcome, settings, ws, base=base)
        title = "chore: daily curation ({0}) - {1}".format(today_iso(), recipe)
        code, out, err = ws._gh(
            "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body
        )
        if code != 0:
            outcome.messages.append(
                "gh pr create failed: {0}".format(truncate(err or out, 300))
            )
            return None
        url = ""
        for line in out.splitlines():
            if line.strip().startswith("http"):
                url = line.strip()
                break
        ws.add_labels(
            [
                settings.labels.get("maintenance", ""),
                settings.labels.get("audit", ""),
            ],
            branch,
        )
        outcome.messages.append(
            "opened curation pull request: {0}".format(url or "(url unavailable)")
        )
        return url or "created"


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


@dataclass
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    status: str  # ok | warn | fail
    detail: str
    hint: str = ""

    def to_dict(self) -> Dict[str, str]:
        """Serialise to a plain dictionary (for JSON output)."""
        return {"name": self.name, "status": self.status, "detail": self.detail, "hint": self.hint}


def _check_network(timeout: int = 10) -> Tuple[bool, str]:
    """Probe GitHub's API endpoint to confirm outbound connectivity."""
    code, out, err = run(
        [
            "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
            "--max-time", str(timeout), "https://api.github.com/",
        ],
        timeout=timeout + 5,
    )
    if code != 0:
        return False, "unreachable: {0}".format(truncate(err or out, 120))
    if out.strip() == "200":
        return True, "api.github.com reachable (HTTP 200)"
    return False, "unexpected HTTP status {0}".format(out.strip() or "unknown")


def _check_writable(path: Path) -> Tuple[bool, str]:
    """Return whether ``path`` is (or can become) a writable directory."""
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    if not target.exists():
        return False, "no existing parent directory"
    if not os.access(str(target), os.W_OK):
        return False, "{0} is not writable".format(target)
    return True, "{0} is writable".format(target)


def run_diagnostics(config_path: Path) -> List[CheckResult]:
    """Run every pre-flight diagnostic check and return the results."""
    results: List[CheckResult] = []

    # 1) Interpreter -------------------------------------------------------- #
    version = sys.version_info
    if version >= (3, 8):
        results.append(
            CheckResult(
                "python",
                "ok",
                "Python {0}.{1}.{2} at {3}".format(
                    version.major, version.minor, version.micro, sys.executable
                ),
            )
        )
    else:
        results.append(
            CheckResult(
                "python",
                "fail",
                "Python {0}.{1} is too old; 3.8 or newer is required".format(
                    version.major, version.minor
                ),
                hint="Install a newer Python and re-run.",
            )
        )

    # 2) Local modules ------------------------------------------------------ #
    try:
        import skill_manager as _sm  # noqa: F401

        results.append(
            CheckResult(
                "skill_manager",
                "ok",
                "importable from {0}".format(relative_to_cwd(_HERE / "skill_manager.py")),
            )
        )
    except Exception as exc:  # pragma: no cover - defensive
        results.append(
            CheckResult(
                "skill_manager",
                "fail",
                "import failed: {0}".format(exc),
                hint="Keep skill_manager.py next to repo_maintainer.py.",
            )
        )

    # 3) Configuration ------------------------------------------------------ #
    config: Optional[Dict[str, Any]] = None
    try:
        config = load_config(config_path)
        entries = config.get("repositories", [])
        enabled = [
            item.get("name")
            for item in entries
            if isinstance(item, dict) and item.get("enabled", True)
        ]
        results.append(
            CheckResult(
                "repos.json",
                "ok",
                "{0} valid: {1} repositor(ies), {2} enabled".format(
                    relative_to_cwd(Path(config_path)), len(entries), len(enabled)
                ),
            )
        )
    except ConfigError as exc:
        results.append(
            CheckResult(
                "repos.json",
                "fail",
                str(exc),
                hint="Fix the configuration file before running a phase.",
            )
        )

    # 4) Schema back-reference ---------------------------------------------- #
    if config is not None:
        schema_ref = config.get("$schema")
        if not schema_ref:
            results.append(CheckResult("schema", "ok", "no $schema reference declared"))
        else:
            schema_path = (Path(config_path).parent / str(schema_ref)).resolve()
            if schema_path.is_file():
                results.append(
                    CheckResult(
                        "schema", "ok", "schema found at {0}".format(relative_to_cwd(schema_path))
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "schema",
                        "warn",
                        "$schema points at a missing file: {0}".format(schema_ref),
                        hint="Add config/repos.schema.json or drop the $schema key.",
                    )
                )

    # 5) Curator recipes (Phase 4) ------------------------------------------ #
    if curators is None:
        results.append(
            CheckResult(
                "curators",
                "fail",
                "the curators package could not be imported: {0}".format(CURATORS_IMPORT_ERROR),
                hint="Fix the import error in curators/; --mode curate is unavailable until then.",
            )
        )
    else:
        results.append(
            CheckResult(
                "curators",
                "ok",
                "{0} recipe(s) registered: {1}".format(
                    len(curators.RECIPE_IDS), ", ".join(curators.RECIPE_IDS)
                ),
            )
        )
        bound: List[str] = []
        unbound: List[str] = []
        for entry in (config or {}).get("repositories", []) or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "?")
            block = entry.get("curator") or {}
            recipe = str(block.get("recipe") or "").strip()
            if recipe and block.get("enabled", True):
                bound.append("{0} -> {1}".format(name, recipe))
            else:
                unbound.append(name)
        results.append(
            CheckResult(
                "curator bindings",
                "ok" if bound else "warn",
                "; ".join(bound) if bound else "no repository declares a curator.recipe",
                hint="" if bound else "Add a curator block to a repository to enable --mode curate.",
            )
        )
        if unbound:
            results.append(
                CheckResult(
                    "curator coverage",
                    "ok",
                    "{0} repository(ies) are maintenance-only: {1}".format(
                        len(unbound), ", ".join(unbound)
                    ),
                )
            )

    # 6) Creative synthesis -------------------------------------------------- #
    if curators is not None:
        try:
            from curators.llm import DEFAULT_MODEL, load_api_key

            has_key = bool(load_api_key())
            results.append(
                CheckResult(
                    "gemini",
                    "ok" if has_key else "warn",
                    "{0} key resolved; creative synthesis enabled".format(DEFAULT_MODEL)
                    if has_key
                    else "no key in ~/.hermes/idea-dump/keys.env; recipes use their offline paths",
                    hint=""
                    if has_key
                    else "Add GEMINI_API_KEY=<key> to ~/.hermes/idea-dump/keys.env for synthesis.",
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            results.append(
                CheckResult("gemini", "warn", "could not inspect the client: {0}".format(exc))
            )

    # 7) git ---------------------------------------------------------------- #
    git_path = which("git")
    if not git_path:
        results.append(
            CheckResult(
                "git",
                "fail",
                "git not found on PATH",
                hint="Install git; all three phases require it.",
            )
        )
    else:
        code, out, _err = run(["git", "--version"], timeout=15)
        results.append(
            CheckResult("git", "ok", out.strip() if code == 0 else git_path)
        )

    # 8) GitHub CLI --------------------------------------------------------- #
    if not which("gh"):
        results.append(
            CheckResult(
                "gh",
                "fail",
                "GitHub CLI not found on PATH",
                hint="Install gh and run `gh auth login`.",
            )
        )
    else:
        authed, detail = gh_auth_ok()
        if authed:
            results.append(CheckResult("gh", "ok", detail))
        else:
            results.append(
                CheckResult(
                    "gh",
                    "warn",
                    detail,
                    hint="Phase 1 still works unauthenticated; phases 2 and 3 need auth.",
                )
            )

    # 9) Network ------------------------------------------------------------- #
    if which("curl"):
        reachable, detail = _check_network()
        results.append(
            CheckResult(
                "network",
                "ok" if reachable else "fail",
                detail,
                hint="" if reachable else "Cloning and API calls will fail.",
            )
        )
    else:
        results.append(CheckResult("network", "warn", "curl not available; probe skipped"))

    # 10) Optional tooling --------------------------------------------------- #
    for binary, purpose in (
        ("npm", "npm dependency audits and patch bumps"),
        ("ruff", "Python formatting and lint autofix"),
        ("npx", "Open Skills CLI search (Phase 3)"),
    ):
        path = which(binary)
        if path:
            results.append(CheckResult(binary, "ok", "found at {0} ({1})".format(path, purpose)))
        else:
            results.append(
                CheckResult(
                    binary, "warn", "{0} not found; {1} will be skipped".format(binary, purpose)
                )
            )

    # 11) Writable directories ----------------------------------------------- #
    for label, path in (
        ("workspace", ROOT_DIR / WORKSPACE_DIRNAME),
        ("reports", ROOT_DIR / REPORTS_DIRNAME),
        ("logs", ROOT_DIR / LOGS_DIRNAME),
        ("skill staging", skill_manager.default_staging_dir()),
    ):
        writable, detail = _check_writable(path)
        results.append(
            CheckResult(
                label,
                "ok" if writable else "warn",
                detail,
                hint="" if writable else "It will be created on demand.",
            )
        )

    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

_STATUS_ICON = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}


def print_diagnostics(results: Sequence[CheckResult], *, as_json: bool) -> int:
    """Print diagnostics as a table (or JSON) and return a process exit code."""
    if as_json:
        print(json.dumps([item.to_dict() for item in results], indent=2))
    else:
        print("")
        print("repo-maintainer pre-flight diagnostics")
        print("=" * 78)
        for item in results:
            print("[{0:>4}] {1:<14} {2}".format(_STATUS_ICON.get(item.status, "?"), item.name, item.detail))
            if item.hint and item.status != "ok":
                print("       {0:<14} -> {1}".format("", item.hint))
        print("=" * 78)
        failed = sum(1 for item in results if item.status == "fail")
        warned = sum(1 for item in results if item.status == "warn")
        print(
            "{0} check(s): {1} passed, {2} warning(s), {3} failure(s)".format(
                len(results), len(results) - failed - warned, warned, failed
            )
        )
        print("")

    return 1 if any(item.status == "fail" for item in results) else 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="repo_maintainer.py",
        description=(
            "Autonomous multi-phase maintenance engine for GitHub repositories: "
            "observer audits, automated pull requests, and feature/skill expansion."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 repo_maintainer.py --check-only\n"
            "  python3 repo_maintainer.py --mode observer\n"
            "  python3 repo_maintainer.py --mode pr --repo owner/name --dry-run\n"
            "  python3 repo_maintainer.py --mode feature --repo owner/name\n"
            "  python3 repo_maintainer.py --curate --repo owner/name --dry-run\n"
            "  python3 repo_maintainer.py --list-curators\n"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=list(ALL_MODES),
        default=MODE_OBSERVER,
        help="Execution phase to run (default: observer).",
    )
    parser.add_argument(
        "--curate",
        action="store_true",
        help="Shorthand for --mode curate: run each repository's daily expansion recipe.",
    )
    parser.add_argument(
        "--no-llm",
        dest="no_llm",
        action="store_true",
        help="Force every curator onto its deterministic offline path (no Gemini calls).",
    )
    parser.add_argument(
        "--list-curators",
        dest="list_curators",
        action="store_true",
        help="Print the registered curator recipes and exit.",
    )
    parser.add_argument(
        "--repo",
        dest="repo",
        metavar="REPO_NAME",
        default=None,
        help="Run on one configured repository (default: all enabled repositories).",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run pre-flight diagnostics (gh auth, network, config) and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the audit and proposed changes without writing files or opening PRs.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        metavar="PATH",
        help="Path to repos.json (default: config/repos.json).",
    )
    parser.add_argument(
        "--workspace-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Directory for isolated clones (default: workspace/).",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Directory for health reports (default: reports/).",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Emit debug-level detail on stderr.",
    )
    return parser


def print_summary(outcomes: Sequence[RepoOutcome], *, as_json: bool) -> None:
    """Print a per-repository summary for a completed run."""
    if as_json:
        print(
            json.dumps(
                {
                    "generated_at": utcnow().isoformat(),
                    "outcomes": [item.to_dict() for item in outcomes],
                },
                indent=2,
            )
        )
        return

    print("")
    print("=" * 78)
    print("Run summary")
    print("=" * 78)
    for outcome in outcomes:
        print("")
        print("{0}  [{1}]".format(outcome.repo, outcome.status))
        if outcome.report_path:
            print("  report : {0}".format(relative_to_cwd(outcome.report_path)))
        if outcome.branch:
            print("  branch : {0}".format(outcome.branch))
        if outcome.commit:
            print("  commit : {0}".format(outcome.commit[:12]))
        if outcome.pr_url:
            print("  pr     : {0}".format(outcome.pr_url))
        if outcome.proposals:
            print("  props  : {0}".format(", ".join(outcome.proposals)))
        if outcome.curation.get("recipe"):
            print("  recipe : {0}".format(outcome.curation["recipe"]))
            for item in (outcome.curation.get("items") or [])[:10]:
                print(
                    "  + {0}{1}".format(
                        item.get("title", ""),
                        " -> {0}".format(item["path"]) if item.get("path") else "",
                    )
                )
            for problem in outcome.curation.get("verify") or []:
                print("  ! {0}".format(problem))
        for message in outcome.messages:
            print("  - {0}".format(message))
        for change in outcome.changes:
            print("  * {0}: {1}".format(change.category, change.summary))
        for skill in outcome.skills:
            print(
                "  # skill {0} -> {1}{2}".format(
                    skill.get("domain"),
                    skill.get("action"),
                    " ({0})".format(skill.get("name")) if skill.get("name") else "",
                )
            )
        if outcome.error:
            print("  ! error: {0}".format(outcome.error))
    print("")
    failed = sum(1 for item in outcomes if item.status == "failed")
    print("{0} repositor(ies) processed, {1} failed.".format(len(outcomes), failed))
    print("")


def print_curators(*, as_json: bool) -> int:
    """Print the registered curator recipes; returns a process exit code."""
    if curators is None:
        _log("curators package unavailable: {0}".format(CURATORS_IMPORT_ERROR))
        return 1
    recipes = curators.known_recipes()
    if as_json:
        print(json.dumps(recipes, indent=2))
        return 0
    print("")
    print("registered curator recipes ({0})".format(len(recipes)))
    print("=" * 78)
    for recipe in recipes:
        print("{0:<16} {1}".format(recipe["id"], recipe["title"]))
        print("{0:<16} {1}".format("", recipe["summary"]))
    print("=" * 78)
    print("Bind one to a repository with curator.recipe in config/repos.json.")
    print("")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``python3 repo_maintainer.py``."""
    args = _build_parser().parse_args(argv)
    set_verbose(bool(args.verbose))
    config_path = Path(args.config)
    mode = MODE_CURATE if args.curate else args.mode

    if args.check_only:
        return print_diagnostics(run_diagnostics(config_path), as_json=bool(args.as_json))
    if args.list_curators:
        return print_curators(as_json=bool(args.as_json))

    try:
        config = load_config(config_path)
        settings_list, notes = select_repositories(config, repo_filter=args.repo, mode=mode)
    except ConfigError as exc:
        _log("configuration error: {0}".format(exc))
        return 2

    if not settings_list:
        _log("no repositories matched the selection")
        return 2

    maintainer = Maintainer(
        config,
        config_path=config_path,
        workspace_root=args.workspace_dir,
        reports_root=args.reports_dir,
        dry_run=bool(args.dry_run),
    )

    if args.dry_run:
        _log("DRY RUN: no files written, no branch pushed, no PR opened")
    _log(
        "mode={0} repositories={1}{2}".format(
            mode, len(settings_list), " (dry-run)" if args.dry_run else ""
        )
    )
    if mode == MODE_CURATE and args.no_llm:
        _log("synthesis disabled: curators will use their deterministic offline paths")
    for note in notes:
        _log(note)

    runners = {
        MODE_OBSERVER: maintainer.run_observer,
        MODE_PR: maintainer.run_pr,
        MODE_FEATURE: maintainer.run_feature,
    }

    outcomes: List[RepoOutcome] = []
    try:
        for settings in settings_list:
            try:
                if mode == MODE_CURATE:
                    outcomes.append(
                        maintainer.run_curate(settings, use_llm=not args.no_llm)
                    )
                else:
                    outcomes.append(runners[mode](settings))
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # isolate per-repository failures
                _log("unhandled error for {0}: {1}".format(settings.name, exc))
                failed = RepoOutcome(repo=settings.name, mode=mode, status="failed")
                failed.error = truncate(str(exc), 400)
                outcomes.append(failed)
    finally:
        maintainer.cleanup()

    print_summary(outcomes, as_json=bool(args.as_json))
    return 1 if any(item.status == "failed" for item in outcomes) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
