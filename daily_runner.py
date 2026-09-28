#!/usr/bin/env python3
"""Sequential daily driver for autonomous repository curation.

``repo_maintainer.py --mode curate`` curates a single repository per process.
This script is what a scheduler calls: it walks every enabled repository in
``config/repos.json`` **in sequence**, spaces the runs out, survives one
repository failing, and writes a run summary an operator can read in the
morning.

Design notes
------------
* **One repository per child process.** Each repository is curated by a
  separate ``python3 repo_maintainer.py --mode curate`` invocation. A recipe
  that segfaults, hangs, or exhausts memory takes down only its own process;
  the remaining repositories still run. This is why the runner shells out
  instead of importing :class:`repo_maintainer.Maintainer` directly.
* **A lock, so schedules cannot overlap.** ``launchd`` will happily start a
  second run if the first one is still going (a long ``--harvest_timeout``
  is easy to outlast). An exclusive ``flock`` on ``logs/daily-runner.lock``
  makes a second invocation exit immediately instead of doubling the load
  on GitHub and the Gemini free tier.
* **Explicit rate limiting.** ``--interval`` (default 60s) is applied between
  repositories. Repositories run sequentially on purpose: parallel runs
  would trip GitHub secondary rate limits and make the free-tier LLM budget
  unpredictable.
* **The same guardrails as a manual run.** Guardrails are not re-implemented
  here; every child is a plain ``repo_maintainer.py`` call, so branch
  protection, preflight, and post-condition verification all still apply.

CLI examples
------------
    python3 daily_runner.py --dry-run
    python3 daily_runner.py --repo knarayanareddy/AI-Daily
    python3 daily_runner.py --install-launchd
    python3 daily_runner.py --uninstall-launchd
    launchctl kickstart -k gui/$(id -u)/com.antigravity.repo-maintainer
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_HERE))

import repo_maintainer  # noqa: E402  local module, resolved through _HERE

__all__ = ["DailyRunResult", "RunLock", "main"]

ROOT_DIR = _HERE
DEFAULT_CONFIG_PATH = ROOT_DIR / "config" / "repos.json"
LOGS_DIRNAME = "logs"
REPORTS_DIRNAME = "reports"
LOCK_FILENAME = "daily-runner.lock"
PLIST_DIRNAME = "launchd"
PLIST_FILENAME = "com.antigravity.repo-maintainer.plist"
PLIST_LABEL = "com.antigravity.repo-maintainer"

#: Seconds of spacing between repositories; respects GitHub's rate limits.
DEFAULT_INTERVAL_SECONDS = 60

#: Hard ceiling on one repository's child process, so a wedged harvest fails
#: the repository instead of blocking the whole nightly run.
DEFAULT_REPO_TIMEOUT = 3600


def _log(message: str) -> None:
    """Emit a timestamped progress line on stderr (stdout stays machine-readable)."""
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print("[{0}] {1}".format(stamp, message), file=sys.stderr, flush=True)



class RunLock:
    """Exclusive advisory lock preventing overlapping daily runs.

    ``flock`` is released automatically when the process exits, including on
    a crash, so a killed run can never leave a stale lock behind.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: Optional[Any] = None

    def acquire(self) -> bool:
        """Try to take the lock; ``False`` means another run is in progress."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - closed in release
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError):
            if not self._acquire_pid(handle):
                handle.close()
                return False
        handle.seek(0)
        handle.truncate()
        handle.write("{0} pid={1}\n".format(_timestamp(), os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def _acquire_pid(self, handle: Any) -> bool:
        """Fallback lock used only where ``flock`` is unavailable."""
        handle.seek(0)
        existing = handle.read().strip()
        if existing:
            try:
                pid = int(existing.rsplit("=", 1)[-1])
            except ValueError:
                return True
            try:
                os.kill(pid, 0)
            except OSError:
                return True  # stale: the recorded process is gone
            return False
        return True

    def release(self) -> None:
        """Release the lock if this instance holds it."""
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):  # pragma: no cover - defensive
            pass
        try:
            self._handle.close()
        finally:
            self._handle = None

    def __enter__(self) -> "RunLock":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()


def _timestamp() -> str:
    """Current UTC instant as an ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_slug(name: str) -> str:
    """Filesystem-safe per-repository identifier for log and report names."""
    return repo_maintainer.repo_slug(name)


def run_repository(
    name: str,
    *,
    python: str,
    config_path: Path,
    mode: str,
    dry_run: bool,
    timeout: int,
    no_llm: bool = False,
    workspace_root: Optional[Path] = None,
    log_path: Optional[Path] = None,
    as_json: bool = True,
) -> Dict[str, Any]:
    """Curate one repository in a child process and return its outcome.

    The child is ``repo_maintainer.py``, so every Phase 4 guardrail applies
    unchanged. Its stdout is captured for a machine-readable summary while
    stderr (the progress log) is mirrored into ``log_path``.
    """
    cmd: List[str] = [
        python,
        str(ROOT_DIR / "repo_maintainer.py"),
        "--mode",
        mode,
        "--repo",
        name,
        "--config",
        str(config_path),
    ]
    if dry_run:
        cmd.append("--dry-run")
    if no_llm and mode == repo_maintainer.MODE_CURATE:
        cmd.append("--no-llm")
    if as_json:
        cmd.append("--json")
    if workspace_root:
        cmd.extend(["--workspace-dir", str(workspace_root)])

    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never shell=True
            cmd,
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "repo": name,
            "status": "failed",
            "error": "timed out after {0}s".format(timeout),
            "duration_seconds": round(time.monotonic() - started, 1),
            "command": " ".join(cmd),
        }
    except OSError as exc:
        return {
            "repo": name,
            "status": "failed",
            "error": "could not start the child process: {0}".format(exc),
            "duration_seconds": round(time.monotonic() - started, 1),
            "command": " ".join(cmd),
        }

    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("=== {0} :: {1} ===\n".format(name, _timestamp()))
                handle.write(stderr)
                if stdout.strip():
                    handle.write(stdout)
        except OSError:  # pragma: no cover - logging must never abort a run
            pass

    outcome: Dict[str, Any] = {
        "repo": name,
        "status": "failed" if completed.returncode else "ok",
        "returncode": completed.returncode,
        "duration_seconds": round(time.monotonic() - started, 1),
        "command": " ".join(cmd),
    }
    # ``--json`` puts a structured summary on stdout; parse it when present so
    # the daily report can name the recipe, the branch, and the PR.
    try:
        parsed = json.loads(stdout)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("outcomes"), list):
        for item in parsed["outcomes"]:
            if isinstance(item, dict) and item.get("repo") == name:
                outcome["status"] = str(item.get("status") or outcome["status"])
                outcome["branch"] = item.get("branch")
                outcome["pr_url"] = item.get("pr_url")
                outcome["recipe"] = (item.get("curation") or {}).get("recipe")
                outcome["changes"] = len(item.get("changes") or [])
                outcome["error"] = str(item.get("error") or "")
                outcome["messages"] = list(item.get("messages") or [])
                outcome["evaluation"] = (item.get("curation") or {}).get("evaluation")
                outcome["self_correction"] = (item.get("curation") or {}).get("self_correction")
    # A reported error is authoritative regardless of the child's exit code.
    if outcome.get("error"):
        outcome["status"] = "failed"
    return outcome


def render_summary_markdown(
    results: Sequence[Dict[str, Any]],
    *,
    mode: str,
    started_at: str,
    finished_at: str,
    dry_run: bool,
) -> str:
    """Render the end-of-run digest written to ``reports/``."""
    failed = [item for item in results if item.get("status") == "failed"]
    lines: List[str] = [
        "# Daily curation run ({0})".format(started_at[:10]),
        "",
        "| | |",
        "| --- | --- |",
        "| Mode | `{0}`{1} |".format(mode, " (dry-run)" if dry_run else ""),
        "| Started | {0} |".format(started_at),
        "| Finished | {0} |".format(finished_at),
        "| Repositories | {0} |".format(len(results)),
        "| Failed | {0} |".format(len(failed)),
        "",
        "## Results",
        "",
        "| Repository | Status | Recipe | Changes | Evaluation (Jev/Gemini) | Branch / PR | Duration |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in results:
        where = item.get("pr_url") or item.get("branch") or "-"
        ev = item.get("evaluation") or {}
        if ev:
            ev_str = "`{0}` ({1}/10)".format(ev.get("verdict", "APPROVED"), ev.get("quality_score", "-"))
        elif item.get("self_correction") and item["self_correction"].get("healed"):
            ev_str = "Healed ({0} att)".format(item["self_correction"].get("total_attempts", 1))
        else:
            ev_str = "-"
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} | {6}s |".format(
                item.get("repo", "?"),
                item.get("status", "?"),
                item.get("recipe") or "-",
                item.get("changes", 0),
                ev_str,
                where,
                item.get("duration_seconds", 0),
            )
        )
    if failed:
        lines.extend(["", "## Failures", ""])
        for item in failed:
            lines.append("- **{0}**: {1}".format(item.get("repo"), item.get("error") or "unknown"))
            tail = (item.get("messages") or [])[-3:]
            for message in tail:
                lines.append("  - {0}".format(message))
    lines.append("")
    return "\n".join(lines)


def run_daily(
    names: Sequence[str],
    *,
    python: str,
    config_path: Path,
    mode: str,
    dry_run: bool,
    interval: int,
    timeout: int,
    no_llm: bool,
    logs_dir: Path,
    reports_dir: Path,
    workspace_root: Optional[Path],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Curate each repository in sequence and return ``(results, summary)``.

    A failure in one repository is recorded and the loop continues: a single
    broken harvest must not cost the other four their daily run.
    """
    started_at = _timestamp()
    date = started_at[:10]
    results: List[Dict[str, Any]] = []
    log_path = logs_dir / "daily_{0}.log".format(date)

    total = len(names)
    for index, name in enumerate(names, start=1):
        _log("[{0}/{1}] curating {2}".format(index, total, name))
        result = run_repository(
            name,
            python=python,
            config_path=config_path,
            mode=mode,
            dry_run=dry_run,
            timeout=timeout,
            no_llm=no_llm,
            workspace_root=workspace_root,
            log_path=log_path,
        )
        results.append(result)
        _log(
            "[{0}/{1}] {2} -> {3} ({4}s)".format(
                index, total, name, result.get("status"), result.get("duration_seconds")
            )
        )
        if result.get("status") == "failed" and result.get("error"):
            _log("    error: {0}".format(result["error"]))
        if index < total and interval > 0:
            # Sequential by design: spacing keeps GitHub's secondary rate limits
            # and the Gemini free tier well inside their budgets.
            _log("    waiting {0}s before the next repository".format(interval))
            time.sleep(interval)

    finished_at = _timestamp()
    summary: Dict[str, Any] = {
        "generated_at": finished_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "mode": mode,
        "dry_run": bool(dry_run),
        "repositories": list(names),
        "results": results,
        "failed": [item["repo"] for item in results if item.get("status") == "failed"],
        "log_path": str(log_path),
    }

    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "daily_run_{0}.json".format(date)).write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        (reports_dir / "daily_run_{0}.md".format(date)).write_text(
            render_summary_markdown(
                results,
                mode=mode,
                started_at=started_at,
                finished_at=finished_at,
                dry_run=dry_run,
            ),
            encoding="utf-8",
        )
        _log("wrote reports/daily_run_{0}.{{json,md}}".format(date))
    except OSError as exc:  # pragma: no cover - a report failure is not fatal
        _log("could not write the run summary: {0}".format(exc))

    return results, summary


def plist_path() -> Path:
    """Location of the launchd template shipped in ``launchd/``."""
    return ROOT_DIR / PLIST_DIRNAME / PLIST_FILENAME


def render_plist(
    *,
    label: str = PLIST_LABEL,
    python: Optional[str] = None,
    hour: int = 9,
    minute: int = 17,
) -> str:
    """Fill the launchd template with real, absolute values for this machine.

    The template in ``launchd/`` ships with ``__PLACEHOLDER__`` markers so it
    can be read and audited on any machine; this function substitutes the
    interpreter, the repository path, and the schedule before it is written to
    ``~/Library/LaunchAgents/``.
    """
    template = plist_path().read_text(encoding="utf-8")
    resolved_python = python or sys.executable or "/usr/bin/python3"
    substitutions = {
        "__LABEL__": label,
        "__PYTHON__": resolved_python,
        "__REPO_PATH__": str(ROOT_DIR),
        "__RUNNER__": str(ROOT_DIR / "daily_runner.py"),
        "__LOG_DIR__": str(ROOT_DIR / LOGS_DIRNAME),
        "__HOME__": str(Path.home()),
        "__HOUR__": str(int(hour)),
        "__MINUTE__": str(int(minute)),
    }
    for marker, value in substitutions.items():
        template = template.replace(marker, value)
    return template


def _launch_agent_path(label: str = PLIST_LABEL) -> Path:
    """Where launchd expects a per-user agent to live."""
    return Path.home() / "Library" / "LaunchAgents" / "{0}.plist".format(label)


#: Matches an unsubstituted ``__MARKER__`` token.
_MARKER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")


def _unsubstituted_markers(value: Any) -> Set[str]:
    """Collect any ``__MARKER__`` tokens left anywhere in a plist structure."""
    found: Set[str] = set()
    if isinstance(value, str):
        found.update(_MARKER_RE.findall(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.update(_MARKER_RE.findall(str(key)))
            found.update(_unsubstituted_markers(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_unsubstituted_markers(item))
    return found


def _rendered_plist_is_complete(content: str) -> bool:
    """Validate a rendered plist and confirm every marker was substituted.

    Parsing is the point: the template's own XML comment legitimately
    contains the word ``__PLACEHOLDER__``, so a substring test would reject a
    perfectly good render. Parsing also proves the result is a property list
    launchd will accept.
    """
    try:
        parsed = plistlib.loads(content.encode("utf-8"))
    except Exception as exc:  # malformed XML or a bad value type
        _log("the rendered plist is not a valid property list: {0}".format(exc))
        return False
    missing = _unsubstituted_markers(parsed)
    if missing:
        _log(
            "the plist template still contains unsubstituted placeholders: {0}".format(
                ", ".join(sorted(missing))
            )
        )
        return False
    return True


def install_launchd(*, label: str, python: Optional[str], hour: int, minute: int) -> int:
    """Render the plist, install it, and load it into the user's launchd domain."""
    target = _launch_agent_path(label)
    try:
        content = render_plist(label=label, python=python, hour=hour, minute=minute)
    except OSError as exc:
        _log("could not read the plist template: {0}".format(exc))
        return 1
    if not _rendered_plist_is_complete(content):
        return 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        _log("could not write {0}: {1}".format(target, exc))
        return 1
    _log("wrote {0}".format(target))

    # bootout first so re-installing is idempotent rather than erroring on a
    # job that is already loaded.
    domain = "gui/{0}".format(os.getuid())
    _launchctl(["bootout", "{0}/{1}".format(domain, target)], check=False)
    result = _launchctl(["bootstrap", domain, str(target)], check=False)
    if result != 0:
        result = _launchctl(["load", "-w", str(target)], check=False)
    if result != 0:
        _log("the job was written but launchd refused to load it")
        _log("load it manually with: launchctl bootstrap {0} {1}".format(domain, target))
        return 1
    _log("loaded {0} (daily at {1:02d}:{2:02d} local time)".format(label, hour, minute))
    _log("run it now with: launchctl kickstart -k {0}/{1}".format(domain, label))
    return 0


def _launchctl(args: Sequence[str], *, check: bool = True) -> int:
    """Run ``launchctl`` and return its exit code (never raises)."""
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never shell=True
            ["launchctl", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log("launchctl {0} failed: {1}".format(args[0], exc))
        return 1
    output = completed.stdout.decode("utf-8", "replace").strip()
    if output:
        _log("launchctl {0}: {1}".format(args[0], output.splitlines()[-1][:160]))
    return completed.returncode


def uninstall_launchd(*, label: str) -> int:
    """Unload and delete the LaunchAgent."""
    target = _launch_agent_path(label)
    domain = "gui/{0}".format(os.getuid())
    _launchctl(["bootout", "{0}/{1}".format(domain, target)], check=False)
    if not target.exists():
        _log("no LaunchAgent at {0}; nothing to remove".format(target))
        return 0
    try:
        target.unlink()
    except OSError as exc:
        _log("could not remove {0}: {1}".format(target, exc))
        return 1
    _log("removed {0}".format(target))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="daily_runner.py",
        description=(
            "Sequential daily driver for autonomous repository curation. Walks every "
            "enabled repository in config/repos.json, spaces the runs out, isolates "
            "failures, and writes a run summary."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 daily_runner.py --dry-run\n"
            "  python3 daily_runner.py --repo knarayanareddy/AI-Daily\n"
            "  python3 daily_runner.py --list\n"
            "  python3 daily_runner.py --install-launchd --hour 9 --minute 17\n"
            "  python3 daily_runner.py --uninstall-launchd\n"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        metavar="PATH",
        help="Path to repos.json (default: config/repos.json).",
    )
    parser.add_argument(
        "--repo",
        dest="repos",
        metavar="REPO_NAME",
        action="append",
        default=None,
        help="Curate only this repository (repeatable). Default: every enabled one.",
    )
    parser.add_argument(
        "--mode",
        choices=list(repo_maintainer.ALL_MODES),
        default=repo_maintainer.MODE_CURATE,
        help="Phase to run per repository (default: curate).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        metavar="SECONDS",
        help="Spacing between repositories to respect rate limits (default: {0}).".format(
            DEFAULT_INTERVAL_SECONDS
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_REPO_TIMEOUT,
        metavar="SECONDS",
        help="Hard ceiling on one repository (default: {0}).".format(DEFAULT_REPO_TIMEOUT),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview every repository: no writes, no pushes, no pull requests.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Force every curator onto its deterministic offline path (no Gemini calls).",
    )
    parser.add_argument(
        "--workspace-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Directory for isolated clones (default: workspace/ under the repo).",
    )
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=ROOT_DIR / LOGS_DIRNAME,
        metavar="PATH",
        help="Directory for per-run logs (default: logs/).",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=ROOT_DIR / REPORTS_DIRNAME,
        metavar="PATH",
        help="Directory for run summaries (default: reports/).",
    )
    parser.add_argument(
        "--no-lock",
        dest="use_lock",
        action="store_false",
        help="Do not take the overlap lock (for debugging two runs at once).",
    )
    parser.add_argument(
        "--list",
        dest="list_only",
        action="store_true",
        help="Print the repositories that would run, then exit.",
    )
    parser.add_argument(
        "--install-launchd",
        action="store_true",
        help="Write and load the LaunchAgent for a daily run.",
    )
    parser.add_argument(
        "--uninstall-launchd",
        action="store_true",
        help="Unload and delete the LaunchAgent.",
    )
    parser.add_argument(
        "--print-plist",
        action="store_true",
        help="Print the resolved LaunchAgent plist without installing it.",
    )
    parser.add_argument(
        "--label",
        default=PLIST_LABEL,
        metavar="LABEL",
        help="launchd job label (default: {0}).".format(PLIST_LABEL),
    )
    parser.add_argument(
        "--hour",
        type=int,
        default=9,
        metavar="H",
        help="Local hour for the daily launchd run (default: 9).",
    )
    parser.add_argument(
        "--minute",
        type=int,
        default=17,
        metavar="M",
        help="Local minute for the daily launchd run (default: 17).",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Print the machine-readable run summary to stdout.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Emit debug-level detail on stderr.",
    )
    return parser



def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``python3 daily_runner.py``."""
    args = _build_parser().parse_args(argv)
    repo_maintainer.set_verbose(bool(args.verbose))
    config_path = Path(args.config)

    # -- launchd management short-circuits before any repository work -------- #
    if args.print_plist:
        print(render_plist(label=args.label, hour=args.hour, minute=args.minute))
        return 0
    if args.uninstall_launchd:
        return uninstall_launchd(label=args.label)
    if args.install_launchd:
        return install_launchd(
            label=args.label, python=None, hour=args.hour, minute=args.minute
        )

    # -- resolve the work list ----------------------------------------------- #
    try:
        config = repo_maintainer.load_config(config_path)
        selected, notes = repo_maintainer.select_repositories(
            config,
            repo_filter=(args.repos[0] if args.repos and len(args.repos) == 1 else None),
            mode=args.mode,
        )
    except repo_maintainer.ConfigError as exc:
        _log("configuration error: {0}".format(exc))
        return 2

    names = [item.name for item in selected]
    if args.repos:
        wanted = set(args.repos)
        names = [
            name
            for name in names
            if name in wanted or _repo_slug(name) in wanted or _repo_slug(name) == name
        ]
        missing = wanted - set(names)
        if missing:
            _log("no configured repository matched: {0}".format(", ".join(sorted(missing))))
            return 2

    for note in notes:
        _log(note)
    if not names:
        _log("no repositories matched the selection")
        return 2

    if args.list_only:
        _log("{0} repository(ies) selected for mode '{1}':".format(len(names), args.mode))
        for name in names:
            recipe = next(
                (item.curator_recipe() for item in selected if item.name == name), ""
            )
            _log("  - {0}{1}".format(name, "  [{0}]".format(recipe) if recipe else ""))
        return 0

    if args.mode == repo_maintainer.MODE_CURATE and repo_maintainer.curators is None:
        _log(
            "the curators package is unusable, so mode 'curate' cannot run: {0}".format(
                repo_maintainer.CURATORS_IMPORT_ERROR
            )
        )
        return 2

    lock = RunLock(Path(args.logs_dir) / LOCK_FILENAME)
    if args.use_lock:
        if not lock.acquire():
            _log(
                "another daily run holds {0}; exiting without duplicating work".format(lock.path)
            )
            return 0

    _log(
        "daily run: mode={0} repositories={1}{2} interval={3}s".format(
            args.mode, len(names), " (dry-run)" if args.dry_run else "", args.interval
        )
    )
    try:
        results, summary = run_daily(
            names,
            python=sys.executable or "python3",
            config_path=config_path,
            mode=args.mode,
            dry_run=bool(args.dry_run),
            interval=max(0, args.interval),
            timeout=max(30, args.timeout),
            no_llm=bool(args.no_llm),
            logs_dir=Path(args.logs_dir),
            reports_dir=Path(args.reports_dir),
            workspace_root=args.workspace_dir,
        )
    finally:
        lock.release()

    if args.as_json:
        print(json.dumps(summary, indent=2))
    else:
        print("")
        print("=" * 78)
        print("Daily run summary ({0})".format(summary["generated_at"]))
        print("=" * 78)
        for item in results:
            eval_info = ""
            ev = item.get("evaluation") or {}
            sc = item.get("self_correction") or {}
            if ev:
                eval_info = " [eval: {0}/10 {1} via {2}]".format(
                    ev.get("quality_score", "-"),
                    ev.get("verdict", "APPROVED"),
                    ev.get("evaluator_model", "jev/gemini"),
                )
            elif sc and sc.get("healed"):
                eval_info = " [self-healed ({0} att)]".format(sc.get("total_attempts", 1))
            print(
                "{0:<40} {1:<10} {2}{3}".format(
                    item.get("repo", "?"),
                    item.get("status", "?"),
                    item.get("pr_url") or item.get("branch") or item.get("error") or "",
                    eval_info,
                )
            )
        print("")
        print(
            "{0} repositor(ies) processed, {1} failed. Log: {2}".format(
                len(results), len(summary["failed"]), summary["log_path"]
            )
        )
        print("")

    return 1 if summary["failed"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
