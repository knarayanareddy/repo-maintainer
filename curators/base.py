"""Shared foundation for repo-specific autonomous curation recipes.

Every curator implements exactly three verbs, declared on
:class:`CurationRecipe`:

``check(workspace_path)``
    Read-only preflight. Confirms the clone looks like the repository this
    recipe was written for and that the external tools and credentials the
    recipe needs are available. Never writes.

``curate(workspace_path, dry_run=False)``
    Produce the daily expansion. Every mutation is routed through
    :class:`FilePlan`, which buffers writes in memory; nothing reaches disk
    when ``dry_run`` is set, so a preview can never leave partial state behind.

``verify(workspace_path)``
    Read-only post-condition check. Returns a list of human-readable
    problems; an empty list means the curated tree is internally consistent.

The base class also supplies the plumbing every recipe needs: a
``subprocess`` wrapper with the same never-raise contract as
:func:`skill_manager.run`, GitHub REST helpers, deterministic slugs, and a
``log`` callback the CLI wires to stderr.
"""

from __future__ import annotations

import abc
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .llm import GeminiClient

__all__ = [
    "CurationError",
    "CheckItem",
    "CheckReport",
    "CurationItem",
    "CurationResult",
    "PlannedWrite",
    "FilePlan",
    "CurationRecipe",
    "run",
    "slugify",
    "iso_utc_now",
    "utc_date",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Subprocess output cap, mirroring :data:`skill_manager.MAX_COMMAND_OUTPUT`.
MAX_COMMAND_OUTPUT = 20000

#: Default ceiling for any single external command a recipe may run.
DEFAULT_COMMAND_TIMEOUT = 600

#: Default timeout for a single HTTP request.
DEFAULT_HTTP_TIMEOUT = 30

#: Marker pair used to keep machine-maintained blocks idempotent.
CURATION_START = "<!-- repo-maintainer:curation:start -->"
CURATION_END = "<!-- repo-maintainer:curation:end -->"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")


class CurationError(RuntimeError):
    """Raised when a recipe cannot run at all (bad clone, missing tooling)."""


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #


def run(
    cmd: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """Run a command and return ``(returncode, stdout, stderr)``.

    Never raises. A missing executable becomes ``(127, "", msg)`` and a
    timeout becomes ``(124, "", msg)`` so an optional tool degrades into a
    note instead of crashing a daily run.
    """
    printable = " ".join(str(part) for part in cmd)
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never shell=True
            [str(part) for part in cmd],
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", "executable not found: {0}".format(printable)
    except subprocess.TimeoutExpired:
        return 124, "", "command timed out after {0}s: {1}".format(timeout, printable)
    except OSError as exc:  # pragma: no cover - defensive
        return 126, "", "could not execute {0}: {1}".format(printable, exc)

    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    if len(stdout) > MAX_COMMAND_OUTPUT:
        stdout = stdout[:MAX_COMMAND_OUTPUT] + "\n...[truncated]"
    if len(stderr) > MAX_COMMAND_OUTPUT:
        stderr = stderr[:MAX_COMMAND_OUTPUT] + "\n...[truncated]"
    return completed.returncode, stdout, stderr


def slugify(value: str, *, max_length: int = 60, fallback: str = "item") -> str:
    """Lowercase, dash-separated, filesystem-safe slug."""
    slug = _SLUG_STRIP.sub("-", str(value or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or fallback


def collapse_ws(value: str) -> str:
    """Collapse all whitespace runs to single spaces and strip."""
    return _WHITESPACE.sub(" ", str(value or "")).strip()


def truncate(text: str, limit: int = 160) -> str:
    """Shorten ``text`` to ``limit`` characters with an ellipsis."""
    flat = collapse_ws(text)
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)].rstrip() + "…"


def iso_utc_now() -> str:
    """Current UTC instant as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def utc_date() -> str:
    """Current UTC calendar date as ``YYYY-MM-DD``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def compact_date() -> str:
    """Current UTC date as ``YYYYMMDD`` (used in AI-Daily file names)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def parse_iso(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def hours_ago_iso(hours: int) -> str:
    """ISO timestamp ``hours`` in the past (used for 24h curation windows)."""
    moment = datetime.now(timezone.utc) - timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def http_json(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Any], str]:
    """Perform an HTTP request and decode a JSON body.

    Returns ``(data, error)``. Exactly one of the two is meaningful: a network
    or decoding problem yields ``(None, "reason: detail")`` instead of raising,
    because a recipe must degrade rather than abort the daily pipeline.
    """
    body = None
    request_headers = {"User-Agent": "repo-maintainer-curator/1.0", "Accept": "application/json"}
    request_headers.update(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return None, "http {0}: {1}".format(exc.code, collapse_ws(detail) or exc.reason)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, "{0}: {1}".format(type(exc).__name__, exc)
    try:
        return json.loads(raw), ""
    except ValueError as exc:
        return None, "invalid JSON ({0})".format(exc)


def github_token() -> Optional[str]:
    """Resolve a GitHub token from the environment or the ``gh`` keyring.

    Order matters: an explicit environment token wins so a daily run can be
    pinned to a fine-grained token, and ``gh auth token`` is the fallback.
    """
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_API_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    code, out, _err = run(["gh", "auth", "token"], timeout=20)
    if code == 0 and out.strip():
        return out.strip()
    return None

    if len(stdout) > MAX_COMMAND_OUTPUT:
        stdout = stdout[:MAX_COMMAND_OUTPUT] + "\n...[truncated]"
    if len(stderr) > MAX_COMMAND_OUTPUT:
        stderr = stderr[:MAX_COMMAND_OUTPUT] + "\n...[truncated]"
    return completed.returncode, stdout, stderr



# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass
class CheckItem:
    """One line of a preflight checklist."""

    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for JSON output."""
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "fatal": self.fatal}


@dataclass
class CheckReport:
    """Aggregated :meth:`CurationRecipe.check` output."""

    recipe: str
    items: List[CheckItem] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", *, fatal: bool = True) -> CheckItem:
        """Record a checklist line and return it."""
        item = CheckItem(name=name, ok=bool(ok), detail=detail, fatal=fatal)
        self.items.append(item)
        return item

    def require(self, path: Path, label: str, *, relative_to: Optional[Path] = None) -> bool:
        """Record whether ``path`` exists, phrasing the detail relatively."""
        exists = Path(path).exists()
        shown = Path(path)
        if relative_to:
            try:
                shown = Path(path).relative_to(relative_to)
            except ValueError:
                pass
        return self.add(label, exists, "{0}".format(shown))

    @property
    def ok(self) -> bool:
        """True when no fatal check failed."""
        return not self.problems()

    def problems(self) -> List[str]:
        """Every failed fatal check, formatted for a log line."""
        return [
            "{0}: {1}".format(item.name, item.detail or "failed")
            for item in self.items
            if not item.ok and item.fatal
        ]

    def warnings(self) -> List[str]:
        """Every failed non-fatal check, formatted for a log line."""
        return [
            "{0}: {1}".format(item.name, item.detail or "warning")
            for item in self.items
            if not item.ok and not item.fatal
        ]

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for JSON output."""
        return {
            "recipe": self.recipe,
            "ok": self.ok,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass
class CurationItem:
    """A single artefact a recipe produced during :meth:`curate`."""

    kind: str
    title: str
    path: str = ""
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for JSON output."""
        return {"kind": self.kind, "title": self.title, "path": self.path, "detail": self.detail}


@dataclass
class CurationResult:
    """Everything one :meth:`curate` invocation produced."""

    recipe: str
    items: List[CurationItem] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    writes: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    dry_run: bool = False
    llm_used: bool = False

    def add(self, kind: str, title: str, *, path: str = "", detail: str = "") -> CurationItem:
        """Record a produced artefact and return it."""
        item = CurationItem(kind=kind, title=title, path=path, detail=detail)
        self.items.append(item)
        return item

    def note(self, message: str) -> str:
        """Record an operator-facing note and return it."""
        self.notes.append(message)
        return message

    @property
    def changed(self) -> bool:
        """Whether the run would change (or did change) any file."""
        return bool(self.writes)

    @property
    def ok(self) -> bool:
        """A result is usable when nothing it needs to do went wrong."""
        return not self.problems

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for JSON output."""
        return {
            "recipe": self.recipe,
            "items": [item.to_dict() for item in self.items],
            "notes": list(self.notes),
            "writes": list(self.writes),
            "problems": list(self.problems),
            "dry_run": self.dry_run,
            "llm_used": self.llm_used,
        }


# --------------------------------------------------------------------------- #
# Dry-run-safe file mutation
# --------------------------------------------------------------------------- #


@dataclass
class PlannedWrite:
    """A single buffered filesystem mutation."""

    action: str  # write | append | replace-block | delete
    path: str
    content: str = ""

    @property
    def size(self) -> int:
        """Byte length of the payload this write would persist."""
        return len(self.content.encode("utf-8"))


class FilePlan:
    """Buffers every mutation so a dry-run cannot touch the working tree.

    Recipes never call :func:`Path.write_text` directly. They call
    :meth:`write`, :meth:`append`, :meth:`replace_block` or :meth:`delete`;
    the plan applies the batch with :meth:`flush` and is a no-op in dry-run
    mode. Paths are always interpreted relative to the repository root.
    """

    def __init__(self, root: Path, *, dry_run: bool = False) -> None:
        self.root = Path(root)
        self.dry_run = bool(dry_run)
        self.planned: List[PlannedWrite] = []
        self.applied = False

    # -- introspection ------------------------------------------------------ #

    def paths(self) -> List[str]:
        """Repository-relative paths touched by this plan, in order."""
        seen: List[str] = []
        for item in self.planned:
            if item.path not in seen:
                seen.append(item.path)
        return seen

    def total_bytes(self) -> int:
        """Total payload size of the plan, for reporting."""
        return sum(item.size for item in self.planned)

    def is_empty(self) -> bool:
        """Whether nothing is queued."""
        return not self.planned

    # -- resolution --------------------------------------------------------- #

    def resolve(self, relative: str) -> Path:
        """Map a repository-relative path to an absolute one.

        Absolute inputs are rejected on purpose: a recipe that can name an
        arbitrary filesystem path is a recipe that can escape its clone.
        """
        candidate = Path(relative)
        if candidate.is_absolute():
            raise CurationError("curation paths must be relative: {0}".format(relative))
        target = (self.root / candidate).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError as exc:
            raise CurationError(
                "curation path escapes the repository root: {0}".format(relative)
            ) from exc
        return target

    def read(self, relative: str, default: str = "") -> str:
        """Read a repository-relative file, returning ``default`` if absent."""
        try:
            return self.resolve(relative).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, CurationError):
            return default
        except UnicodeDecodeError:  # pragma: no cover - binary guard
            return default

    def read_json(self, relative: str, default: Any = None) -> Any:
        """Read and decode a repository-relative JSON file."""
        raw = self.read(relative)
        if not raw.strip():
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def exists(self, relative: str) -> bool:
        """Whether a repository-relative path exists."""
        try:
            return self.resolve(relative).exists()
        except CurationError:
            return False

    # -- queueing ----------------------------------------------------------- #

    def write(self, relative: str, content: str) -> str:
        """Queue a full-file write and return the relative path."""
        self.planned.append(
            PlannedWrite(action="write", path=str(relative), content=str(content))
        )
        return str(relative)

    def write_json(
        self, relative: str, data: Any, *, indent: int = 2, sort_keys: bool = False
    ) -> str:
        """Queue a JSON file write using the repository's usual formatting."""
        payload = json.dumps(data, indent=indent, sort_keys=sort_keys, ensure_ascii=False)
        return self.write(relative, payload + "\n")

    def append(self, relative: str, content: str) -> str:
        """Queue an append, normalising the newline boundary."""
        if not content:
            return str(relative)
        existing = self.read(relative)
        prefix = "\n" if existing and not existing.endswith("\n") else ""
        self.planned.append(
            PlannedWrite(action="append", path=str(relative), content=prefix + content)
        )
        return str(relative)

    def delete(self, relative: str) -> str:
        """Queue a file deletion."""
        self.planned.append(PlannedWrite(action="delete", path=str(relative)))
        return str(relative)

    def replace_block(
        self,
        relative: str,
        start_marker: str,
        end_marker: str,
        body: str,
    ) -> bool:
        """Replace a marker-delimited block, creating it when absent.

        Returns ``True`` when a change was queued and ``False`` when the file
        already contained exactly this block, which is what makes repeated
        daily runs idempotent instead of duplicating sections.
        """
        existing = self.read(relative)
        payload = str(body).strip("\n")
        new_block = "\n".join([start_marker, payload, end_marker, ""])

        if start_marker in existing and end_marker in existing:
            head, _, rest = existing.partition(start_marker)
            _old, _, tail = rest.partition(end_marker)
            tail = tail.lstrip("\n")
            rebuilt = head.rstrip("\n") + "\n\n" + new_block + ("\n" + tail if tail else "")
        elif not existing.strip():
            rebuilt = new_block
        else:
            rebuilt = existing.rstrip("\n") + "\n\n" + new_block

        if rebuilt == existing:
            return False
        self.planned.append(
            PlannedWrite(action="replace-block", path=str(relative), content=rebuilt)
        )
        return True

    # -- application -------------------------------------------------------- #

    def flush(self) -> List[str]:
        """Apply every queued mutation; returns the paths actually written.

        A no-op when ``dry_run`` is set, which is what makes ``--dry-run``
        curation genuinely side-effect free.
        """
        if self.dry_run or self.applied or not self.planned:
            return []
        written: List[str] = []
        for item in self.planned:
            target = self.resolve(item.path)
            if item.action == "delete":
                if target.exists():
                    target.unlink()
                    written.append(item.path)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.action == "append" and target.exists():
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(item.content)
            else:
                with target.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(item.content)
            written.append(item.path)
        self.applied = True
        return written



# --------------------------------------------------------------------------- #
# Recipe contract
# --------------------------------------------------------------------------- #


class CurationRecipe(abc.ABC):
    """Base class for every repo-specific autonomous curation recipe.

    Subclasses set the three class attributes, implement the three verbs, and
    read their knobs through :meth:`option`. Everything a recipe needs from
    the outside world arrives through the constructor so a recipe stays pure
    enough to be unit tested against a temporary directory.
    """

    #: Stable identifier referenced by ``repos.json`` (``curator.recipe``).
    recipe_id: str = "base"
    #: Human-readable title used in logs and PR bodies.
    title: str = "Curation recipe"
    #: One-line explanation of what the recipe adds per run.
    summary: str = ""

    def __init__(
        self,
        options: Optional[Dict[str, Any]] = None,
        *,
        llm: Optional["GeminiClient"] = None,
        log: Optional[Callable[[str], None]] = None,
        dry_run: bool = False,
    ) -> None:
        self.options: Dict[str, Any] = dict(options or {})
        self._llm = llm
        self._log_fn = log
        self.dry_run = bool(dry_run)
        self.result: Optional[CurationResult] = None

    # -- configuration ------------------------------------------------------ #

    def option(self, key: str, default: Any = None) -> Any:
        """Read a recipe knob, coercing numeric strings to ``int``."""
        if key not in self.options:
            return default
        value = self.options[key]
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if isinstance(default, int) and not isinstance(value, bool):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        if isinstance(default, str) and value is not None:
            return str(value)
        return value if value is not None else default

    @property
    def llm(self) -> Optional["GeminiClient"]:
        """The synthesis client, or ``None`` when the recipe must stay offline."""
        return self._llm

    @property
    def llm_enabled(self) -> bool:
        """Whether LLM synthesis is both requested and actually available."""
        return bool(self.option("llm", True)) and self._llm is not None

    def log(self, message: str) -> None:
        """Emit a progress line through the injected logger."""
        if self._log_fn is not None:
            self._log_fn("[{0}] {1}".format(self.recipe_id, message))

    # -- the three verbs ---------------------------------------------------- #

    @abc.abstractmethod
    def check(self, workspace_path: Path) -> CheckReport:
        """Read-only preflight against a freshly cloned workspace."""

    @abc.abstractmethod
    def curate(self, workspace_path: Path, dry_run: bool = False) -> CurationResult:
        """Produce the daily expansion, honouring ``dry_run`` exactly."""

    @abc.abstractmethod
    def verify(self, workspace_path: Path) -> List[str]:
        """Return post-condition problems; an empty list means healthy."""

    # -- shared helpers ----------------------------------------------------- #

    def finish(self, result: CurationResult, plan: FilePlan) -> CurationResult:
        """Apply (or, in dry-run, merely report) the plan and return the result."""
        result.writes = plan.paths()
        result.dry_run = plan.dry_run
        if plan.dry_run:
            self.log(
                "dry-run: {0} file(s), {1} bytes would be written".format(
                    len(result.writes), plan.total_bytes()
                )
            )
        elif plan.is_empty():
            result.note("no new content this run")
        else:
            plan.flush()
            self.log("wrote {0} file(s)".format(len(plan.paths())))
        self.result = result
        return result

    def python_executable(self) -> str:
        """Interpreter used to run a target repository's Python pipeline."""
        return self.option("python", "python3") or "python3"

    def node_executable(self) -> Optional[str]:
        """Locate ``node`` on PATH, or ``None`` when unavailable."""
        candidate = self.option("node", "node") or "node"
        code, out, _err = run([candidate, "--version"], timeout=20)
        return candidate if code == 0 else None

    def github_search(
        self,
        query: str,
        *,
        limit: int = 30,
        sort: str = "stars",
        token: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Search GitHub repositories and return ``(items, error)``.

        Uses the REST search endpoint. A missing token still works, just under
        the stricter unauthenticated rate limit.
        """
        resolved = token if token is not None else github_token()
        headers = {"Accept": "application/vnd.github+json"}
        if resolved:
            headers["Authorization"] = "Bearer {0}".format(resolved)
        params = urllib.parse.urlencode(
            {
                "q": query,
                "sort": sort,
                "order": "desc",
                "per_page": max(1, min(100, limit)),
            }
        )
        url = "https://api.github.com/search/repositories?{0}".format(params)
        data, error = http_json(url, headers=headers)
        if error or not isinstance(data, dict):
            return [], error or "unexpected search response"
        items = data.get("items")
        return (items if isinstance(items, list) else []), ""

    def command_env(self) -> Dict[str, str]:
        """Environment for a target repository's scripts, token included."""
        env = dict(os.environ)
        token = github_token()
        if token:
            env.setdefault("GITHUB_TOKEN", token)
            env.setdefault("GH_TOKEN", token)
        return env

    def walk_files(self, root: Path, suffix: str = "", limit: int = 4000) -> List[Path]:
        """Collect files under ``root`` in a deterministic, capped order."""
        base = Path(root)
        if not base.exists():
            return []
        found: List[Path] = []
        skip = {".git", "node_modules", "dist", "build", ".next", "__pycache__", "quarantine"}
        for current, dirs, names in os.walk(str(base)):
            dirs[:] = sorted(name for name in dirs if name not in skip and not name.startswith("."))
            for name in sorted(names):
                if suffix and not name.endswith(suffix):
                    continue
                found.append(Path(current) / name)
                if len(found) >= limit:
                    return found
        return found

    # -- shared helpers ----------------------------------------------------- #

    def new_plan(self, workspace_path: Path, *, dry_run: bool = False) -> FilePlan:
        """Create a :class:`FilePlan` rooted at ``workspace_path``."""
        self.dry_run = bool(dry_run)
        return FilePlan(Path(workspace_path), dry_run=self.dry_run)

