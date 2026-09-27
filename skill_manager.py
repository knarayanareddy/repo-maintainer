#!/usr/bin/env python3
"""Dynamic skill discovery, sandboxed installation, and generation.

This module powers Phase 3 of the repo maintenance engine. It is deliberately
import-safe and side-effect free at import time: every filesystem write is
funnelled through explicitly invoked methods.

Capabilities
------------
1. Discovery   -- ``npx skills search <term>`` against the Open Skills CLI.
2. Staging     -- install a discovered skill into a sandboxed directory
                  (``~/.cline/skills/staged/<name>`` by default). Nothing is
                  ever written to a live/activated skills directory by this
                  module; promotion is a human decision.
3. Generation  -- synthesise a new ``SKILL.md`` (YAML frontmatter + body) when
                  no existing skill covers a tool or domain.
4. Validation  -- strict syntax validation before a skill is allowed to be
                  considered for activation.

All external processes are executed through :func:`run` so that dry-run mode,
timeouts, and output truncation are honoured consistently.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "SkillValidationError",
    "SkillRecord",
    "SkillManager",
    "default_staging_dir",
    "parse_frontmatter",
    "render_skill_document",
    "validate_skill_document",
    "parse_search_output",
    "run",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SKILL_FILENAME = "SKILL.md"

#: Allowed frontmatter keys. ``name``/``description`` are required; the rest
#: are optional metadata that tooling may consume.
REQUIRED_FRONTMATTER_KEYS = ("name", "description")
OPTIONAL_FRONTMATTER_KEYS = (
    "version",
    "license",
    "tags",
    "source",
    "generated_by",
    "commands",
    "allowed_tools",
)

MAX_NAME_LENGTH = 64
MIN_DESCRIPTION_LENGTH = 20
MAX_DESCRIPTION_LENGTH = 1024

#: Name must be lowercase, hyphen separated, no leading/trailing hyphen.
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Required body sections for a generated skill to be considered complete.
REQUIRED_SECTIONS = ("When to Use", "Workflow", "Commands", "Safety")

#: Body section headings recognised when checking structural completeness.
_SECTION_HEADING = re.compile(r"^#{2,3}\s+(?P<title>.+?)\s*$", re.MULTILINE)

DEFAULT_SEARCH_TIMEOUT = 120
MAX_COMMAND_OUTPUT = 20000


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class SkillValidationError(ValueError):
    """Raised when a SKILL.md document fails syntax validation."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: List[str] = list(errors)
        super().__init__("; ".join(self.errors) or "skill validation failed")


# --------------------------------------------------------------------------- #
# Process helper
# --------------------------------------------------------------------------- #


def run(
    cmd: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    timeout: int = DEFAULT_SEARCH_TIMEOUT,
    check: bool = False,
) -> Tuple[int, str, str]:
    """Run a command, returning ``(returncode, stdout, stderr)``.

    Never raises :class:`subprocess.CalledProcessError`; callers inspect the
    return code. ``FileNotFoundError`` becomes ``(127, "", msg)`` so that a
    missing optional CLI degrades gracefully instead of crashing.
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
        )
    except FileNotFoundError:
        return 127, "", "executable not found: {0}".format(printable)
    except subprocess.TimeoutExpired:
        return 124, "", "command timed out after {0}s: {1}".format(timeout, printable)

    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    if len(stdout) > MAX_COMMAND_OUTPUT:
        stdout = stdout[:MAX_COMMAND_OUTPUT] + "\n...[truncated]"
    if len(stderr) > MAX_COMMAND_OUTPUT:
        stderr = stderr[:MAX_COMMAND_OUTPUT] + "\n...[truncated]"
    if check and completed.returncode != 0:
        raise RuntimeError(
            "command failed ({0}): {1}\n{2}".format(
                completed.returncode, printable, stderr.strip()
            )
        )
    return completed.returncode, stdout, stderr


# --------------------------------------------------------------------------- #
# Minimal YAML frontmatter handling
# --------------------------------------------------------------------------- #

_FRONTMATTER_FENCE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*\n?", re.DOTALL)


def _split_inline_list(inner: str) -> List[str]:
    """Split ``a, "b, c", d`` while respecting quotes."""
    parts: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    for char in inner:
        if quote:
            buf.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            buf.append(char)
            continue
        if char == ",":
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(char)
    if buf:
        parts.append("".join(buf).strip())
    return [part for part in parts if part]


def _coerce_scalar(raw: str) -> object:
    """Coerce a frontmatter scalar or inline list into a Python object."""
    value = raw.strip()
    if not value:
        return ""
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [_coerce_scalar(part) for part in _split_inline_list(inner)] if inner else []
    lowered = value.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def parse_frontmatter(text: str) -> Tuple[Dict[str, object], str]:
    """Split ``text`` into a ``(frontmatter mapping, body)`` pair.

    Supports the flat ``key: value`` and block-list subset of YAML sufficient
    for skill metadata. Raises :class:`ValueError` when no fence is present.
    """
    match = _FRONTMATTER_FENCE.match(text)
    if not match:
        raise ValueError("missing YAML frontmatter block (expected leading '---')")

    meta: Dict[str, object] = {}
    pending_key: Optional[str] = None
    pending_items: List[str] = []

    for raw_line in match.group("body").splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue

        list_item = re.match(r"^\s*-\s+(?P<item>.+)$", line)
        if list_item and pending_key is not None:
            pending_items.append(_coerce_scalar(list_item.group("item")))
            continue

        key_value = re.match(r"^(?P<key>[A-Za-z0-9_.-]+)\s*:\s*(?P<value>.*)$", line)
        if not key_value:
            continue
        if pending_key is not None and pending_items:
            meta[pending_key] = pending_items
            pending_items = []
        key = key_value.group("key").strip()
        value = key_value.group("value")
        if value.strip() == "":
            # A bare key either opens a block list or is an empty value.
            pending_key = key
            meta.setdefault(key, [])
        else:
            meta[key] = _coerce_scalar(value)
            pending_key = None

    if pending_key is not None and pending_items:
        meta[pending_key] = pending_items

    return meta, text[match.end():]


# --------------------------------------------------------------------------- #
# Skill document rendering / validation
# --------------------------------------------------------------------------- #


def _yaml_scalar(value: str) -> str:
    """Quote a scalar safely for YAML output."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", " ").strip()
    return '"{0}"'.format(escaped)


def render_skill_document(
    *,
    name: str,
    description: str,
    body: str,
    version: str = "0.1.0",
    tags: Optional[Sequence[str]] = None,
    commands: Optional[Sequence[str]] = None,
    source: str = "generated",
    generated_by: str = "repo-maintainer skill_manager",
) -> str:
    """Render a complete ``SKILL.md`` document with YAML frontmatter."""
    lines: List[str] = [
        "---",
        "name: {0}".format(_yaml_scalar(name)),
        "description: {0}".format(_yaml_scalar(description)),
        "version: {0}".format(_yaml_scalar(version)),
        "source: {0}".format(_yaml_scalar(source)),
        "generated_by: {0}".format(_yaml_scalar(generated_by)),
    ]
    if tags:
        lines.append("tags:")
        lines.extend("  - {0}".format(_yaml_scalar(tag)) for tag in tags)
    if commands:
        lines.append("commands:")
        lines.extend("  - {0}".format(_yaml_scalar(cmd)) for cmd in commands)
    lines.append("---")
    lines.append("")
    lines.append(body.strip())
    lines.append("")
    return "\n".join(lines)


def _extract_sections(body: str) -> Dict[str, str]:
    """Map lowercased heading title -> section body text."""
    sections: Dict[str, str] = {}
    matches = list(_SECTION_HEADING.finditer(body))
    for index, match in enumerate(matches):
        title = match.group("title").strip().lower()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.setdefault(title, body[start:end].strip())
    return sections


def validate_skill_document(text: str) -> List[str]:
    """Validate a ``SKILL.md`` document, returning a list of error strings.

    An empty list means the document is valid. Checks performed: frontmatter
    presence, required keys, unknown keys, ``name`` convention, description
    length bounds, a non-empty body containing every required section, and
    balanced fenced code blocks.
    """
    errors: List[str] = []

    try:
        meta, body = parse_frontmatter(text)
    except ValueError as exc:
        return [str(exc)]

    for key in REQUIRED_FRONTMATTER_KEYS:
        if key not in meta or meta[key] in ("", [], None):
            errors.append("missing required frontmatter key: '{0}'".format(key))

    allowed = set(REQUIRED_FRONTMATTER_KEYS) | set(OPTIONAL_FRONTMATTER_KEYS)
    for key in meta:
        if key not in allowed:
            errors.append("unknown frontmatter key: '{0}'".format(key))

    name = meta.get("name")
    if isinstance(name, str) and name:
        if len(name) > MAX_NAME_LENGTH:
            errors.append(
                "name exceeds {0} characters: {1}".format(MAX_NAME_LENGTH, len(name))
            )
        if not NAME_PATTERN.match(name):
            errors.append(
                "name must be lowercase alphanumeric words joined by single "
                "hyphens: '{0}'".format(name)
            )

    description = meta.get("description")
    if isinstance(description, str) and description:
        if len(description) < MIN_DESCRIPTION_LENGTH:
            errors.append(
                "description too short (min {0} chars): {1}".format(
                    MIN_DESCRIPTION_LENGTH, len(description)
                )
            )
        if len(description) > MAX_DESCRIPTION_LENGTH:
            errors.append(
                "description too long (max {0} chars): {1}".format(
                    MAX_DESCRIPTION_LENGTH, len(description)
                )
            )

    if not body.strip():
        errors.append("skill body is empty")
    else:
        sections = _extract_sections(body)
        for required in REQUIRED_SECTIONS:
            key = required.lower()
            if key not in sections:
                errors.append("missing required section: '## {0}'".format(required))
            elif not sections[key]:
                errors.append("section '{0}' is present but empty".format(required))

    if body.count("```") % 2 != 0:
        errors.append("unbalanced fenced code block (odd number of ``` markers)")

    return errors


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class SkillRecord:
    """A discovered, staged, or generated skill."""

    name: str
    description: str = ""
    path: Optional[Path] = None
    origin: str = "unknown"  # discovered | staged | generated
    version: str = "0.1.0"
    tags: List[str] = field(default_factory=list)
    valid: bool = False
    errors: List[str] = field(default_factory=list)
    source_url: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "path": str(self.path) if self.path else None,
            "origin": self.origin,
            "version": self.version,
            "tags": list(self.tags),
            "valid": self.valid,
            "errors": list(self.errors),
            "source_url": self.source_url,
        }


# --------------------------------------------------------------------------- #
# Search-result parsing
# --------------------------------------------------------------------------- #

_SLUG_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)$")
_BULLET_RE = re.compile(r"^(?:[-*+]|[\u2022]|\d+[.)])\s+")


def _looks_like_slug(token: str) -> Optional[str]:
    """Return ``owner/repo`` when ``token`` is a registry-style slug."""
    candidate = token.strip().strip("()[]{}<>,.;:'\"")
    if _SLUG_RE.match(candidate):
        return candidate
    return None


def parse_search_output(text: str, limit: int = 5) -> List[SkillRecord]:
    """Parse ``npx skills search`` output into :class:`SkillRecord` items.

    The Open Skills CLI output format has changed across releases, so this
    parser is intentionally tolerant: it recognises JSON objects, markdown
    links, and plain ``owner/repo`` lines. It never raises.
    """
    results: List[SkillRecord] = []
    seen: set = set()

    def _add(name: str, description: str, url: Optional[str]) -> None:
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        results.append(
            SkillRecord(
                name=name,
                description=description.strip(),
                origin="discovered",
                source_url=url,
            )
        )

    # 1) JSON / NDJSON output -- richest source when the CLI supports --json.
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        name = (
            payload.get("name")
            or payload.get("fullName")
            or payload.get("slug")
            or payload.get("id")
        )
        if isinstance(name, str) and name:
            _add(
                name,
                str(payload.get("description") or ""),
                payload.get("url") or payload.get("html_url"),
            )

    # 2) Markdown links: [label](https://github.com/owner/repo)
    for match in re.finditer(
        r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^\s)]+)\)", text
    ):
        url = match.group("url")
        slug_match = re.search(r"github\.com/([^/\s]+/[^/\s#?]+)", url)
        name = slug_match.group(1) if slug_match else match.group("label").strip()
        if name:
            _add(name, match.group("label").strip(), url)

    # 3) Line oriented: "owner/repo    description"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cleaned = _BULLET_RE.sub("", stripped)
        match = re.match(r"^(?P<slug>\S+)(?:\s+)?(?P<rest>.*)$", cleaned)
        if not match:
            continue
        slug = _looks_like_slug(match.group("slug"))
        if not slug:
            continue
        rest = (match.group("rest") or "").strip()
        if rest.lower().startswith("http"):
            continue
        _add(slug, rest, "https://github.com/{0}".format(slug))

    return results[: max(0, limit)]


def default_staging_dir() -> Path:
    """Return the sandboxed staging directory for skills."""
    override = os.environ.get("REPO_MAINTAINER_SKILL_STAGING_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cline" / "skills" / "staged"


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #


class SkillManager:
    """Discover, stage, generate, and validate skills.

    All staging writes are confined to :attr:`staging_dir`. There is
    deliberately no API that promotes a skill into a live skills directory --
    that remains a human, review-gated decision.
    """

    def __init__(
        self,
        staging_dir: Optional[Path] = None,
        *,
        dry_run: bool = False,
        search_binary: str = "npx",
        search_timeout: int = DEFAULT_SEARCH_TIMEOUT,
    ) -> None:
        self.staging_dir = Path(staging_dir) if staging_dir else default_staging_dir()
        self.dry_run = dry_run
        self.search_binary = search_binary
        self.search_timeout = search_timeout
        self.events: List[str] = []

    # -- internals --------------------------------------------------------- #

    def _log(self, message: str) -> None:
        self.events.append(message)
        print("[skill-manager] {0}".format(message), file=sys.stderr)

    def _target_path(self, name: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "skill"
        return self.staging_dir / safe

    @staticmethod
    def slug_to_name(slug: str) -> str:
        """Convert ``owner/repo`` or ``Some Name`` into a valid skill name."""
        base = slug.split("/")[-1] if "/" in slug else slug
        base = re.sub(r"\.git$", "", base)
        base = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
        base = re.sub(r"-{2,}", "-", base)
        if not base:
            base = "generated-skill"
        if base[0].isdigit():
            base = "s-" + base
        return base[:MAX_NAME_LENGTH].rstrip("-") or "generated-skill"

    # -- capability probe -------------------------------------------------- #

    def search_available(self) -> Tuple[bool, str]:
        """Report whether the Open Skills CLI is reachable through npx."""
        code, _, stderr = run(
            [self.search_binary, "--version"], timeout=min(self.search_timeout, 60)
        )
        if code == 0:
            return True, "npx skills CLI available"
        detail = (
            stderr.strip().splitlines()[-1] if stderr.strip() else "exit {0}".format(code)
        )
        return False, "npx skills CLI unavailable: {0}".format(detail)

    # -- discovery --------------------------------------------------------- #

    def search(
        self, term: str, limit: int = 5, *, include_json: bool = True
    ) -> List[SkillRecord]:
        """Search the Open Skills registry for ``term``.

        Tries the JSON form first, then plain text, then an npx auto-install
        prompt variant, and finally falls back to ``npm search``. Failure at
        every level is reported through the event log and an empty list.
        """
        term = (term or "").strip()
        if not term:
            return []

        base_cmd = [self.search_binary, "skills", "search", term]
        attempts: List[List[str]] = []
        if include_json:
            attempts.append(base_cmd + ["--json"])
        attempts.append(base_cmd)
        attempts.append([self.search_binary, "-y", "skills", "search", term])
        attempts.append(["npm", "search", "--json", term])

        for cmd in attempts:
            code, stdout, _stderr = run(cmd, timeout=self.search_timeout)
            if code != 0 or not stdout.strip():
                continue
            results = self._filter_results(
                parse_search_output(stdout, limit=limit * 2), term, limit
            )
            if results:
                self._log(
                    "search '{0}' -> {1} result(s) via `{2}`".format(
                        term, len(results), " ".join(cmd[:2])
                    )
                )
                return results

        self._log("search for '{0}' returned no usable results".format(term))
        return []

    @staticmethod
    def _filter_results(
        results: Sequence[SkillRecord], term: str, limit: int
    ) -> List[SkillRecord]:
        """Prefer results matching the query; keep the rest as fallback."""
        tokens = {
            tok for tok in re.split(r"[^a-z0-9]+", term.lower()) if len(tok) > 2
        }
        if not tokens:
            return list(results[:limit])
        scored: List[Tuple[int, int, SkillRecord]] = []
        for index, record in enumerate(results):
            haystack = "{0} {1}".format(record.name, record.description).lower()
            score = sum(1 for tok in tokens if tok in haystack)
            scored.append((-score, index, record))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored][:limit]

    # -- staging ----------------------------------------------------------- #

    def stage_skill(
        self,
        name: str,
        *,
        text: Optional[str] = None,
        source_url: Optional[str] = None,
    ) -> SkillRecord:
        """Write a validated skill into the sandboxed staging directory.

        Invalid documents are refused and nothing is written. Re-staging
        identical content is a no-op; changed content backs the previous file
        up to ``SKILL.md.bak`` first.
        """
        errors = validate_skill_document(text or "")
        record = SkillRecord(
            name=name,
            origin="staged",
            source_url=source_url,
            errors=errors,
            valid=not errors,
        )
        if errors:
            self._log("refusing to stage '{0}': {1}".format(name, "; ".join(errors)))
            return record

        meta, _body = parse_frontmatter(text or "")
        record.description = str(meta.get("description") or "")
        record.version = str(meta.get("version") or "0.1.0")
        tags = meta.get("tags")
        record.tags = [str(tag) for tag in tags] if isinstance(tags, list) else []

        target = self._target_path(name)
        skill_file = target / SKILL_FILENAME

        if self.dry_run:
            self._log(
                "[dry-run] would stage skill '{0}' at {1}".format(name, skill_file)
            )
            record.path = skill_file
            return record

        if skill_file.exists():
            if skill_file.read_text(encoding="utf-8") == text:
                self._log("skill '{0}' already staged and up to date".format(name))
                record.path = skill_file
                return record
            backup = skill_file.parent / (SKILL_FILENAME + ".bak")
            shutil.copy2(str(skill_file), str(backup))
            self._log("backed up existing skill to {0}".format(backup))

        target.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(text or "", encoding="utf-8")
        record.path = skill_file
        self._log("staged skill '{0}' -> {1}".format(name, skill_file))
        return record

    @staticmethod
    def _default_body(
        *,
        name: str,
        when_to_use: str,
        workflow: Sequence[str],
        commands: Sequence[str],
        verification: Optional[Sequence[str]] = None,
        safety: Optional[Sequence[str]] = None,
    ) -> str:
        """Assemble a body containing every required section."""
        steps = "\n".join(
            "{0}. {1}".format(i, item) for i, item in enumerate(workflow, 1)
        ) or "1. _TODO: define the workflow._"
        cmds = "\n".join("- `{0}`".format(cmd) for cmd in commands) or "- _none recorded yet_"
        verify = (
            "\n".join("- {0}".format(item) for item in verification)
            if verification
            else "- Confirm each command's real output before trusting it."
        )
        guards = (
            "\n".join("- {0}".format(item) for item in safety)
            if safety
            else "- Operate inside a scratch directory; never against production state."
        )
        template = (
            "# {title}\n\n"
            "## When to Use\n\n"
            "{when}\n\n"
            "## Workflow\n\n"
            "{workflow}\n\n"
            "## Commands\n\n"
            "{commands}\n\n"
            "## Verification\n\n"
            "{verification}\n\n"
            "## Safety\n\n"
            "{safety}\n"
        )
        return template.format(
            title=name,
            when=when_to_use.strip(),
            workflow=steps,
            commands=cmds,
            verification=verify,
            safety=guards,
        )

    def stage_discovered(self, record: SkillRecord) -> SkillRecord:
        """Materialise a discovered skill as a stub in the staging sandbox.

        Only registry metadata is trusted; the body is a clearly marked stub
        that a human or the generation flow must complete.
        """
        name = self.slug_to_name(record.name)
        description = record.description.strip() or (
            "Discovered skill '{0}' from the Open Skills registry. Review and "
            "complete the workflow below before activation.".format(record.name)
        )
        if len(description) < MIN_DESCRIPTION_LENGTH:
            description += " (placeholder description pending review)"

        commands = ["npx skills add {0}".format(record.name)]
        body = self._default_body(
            name=name,
            when_to_use=(
                "Use this skill when working with `{0}` or tooling published "
                "under that registry entry.".format(record.name)
            ),
            workflow=[
                "Review the upstream entry and confirm the commands still apply.",
                "Run `npx skills add {0}` in a sandbox to inspect real behaviour.".format(
                    record.name
                ),
                "Replace this stub with concrete, verified instructions.",
            ],
            commands=commands,
        )
        text = render_skill_document(
            name=name,
            description=description,
            body=body,
            tags=["discovered", "staged"],
            commands=commands,
            source=record.source_url or record.name,
        )
        staged = self.stage_skill(name, text=text, source_url=record.source_url)
        staged.origin = "staged"
        return staged

    # -- generation -------------------------------------------------------- #

    def generate_skill(
        self,
        *,
        domain: str,
        summary: str = "",
        commands: Optional[Sequence[str]] = None,
        workflow: Optional[Sequence[str]] = None,
        when_to_use: Optional[str] = None,
        name: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        extra_sections: Optional[Dict[str, str]] = None,
    ) -> SkillRecord:
        """Synthesise a new, validated ``SKILL.md`` for an uncovered domain.

        The document is validated before staging; if validation fails it is
        returned for inspection but never written to disk.
        """
        slug = self.slug_to_name(name or domain)
        domain = domain.strip() or slug

        description = " ".join((summary or "").split())
        if len(description) < MIN_DESCRIPTION_LENGTH:
            description = (
                "Guidance and verified commands for working with {0}. "
                "Generated by the repo maintainer for currently uncovered "
                "tooling.".format(domain)
            )
        if len(description) > MAX_DESCRIPTION_LENGTH:
            description = description[: MAX_DESCRIPTION_LENGTH - 1].rstrip() + "\u2026"

        when = when_to_use or (
            "Use this skill when a task involves `{0}` and no existing skill "
            "covers it.".format(domain)
        )
        steps = list(workflow) if workflow else [
            "Identify the smallest task that exercises `{0}`.".format(domain),
            "Run the reference commands below and capture their real output.",
            "Record findings in the Verification section of this file.",
            "Promote this skill out of the staging sandbox once verified.",
        ]
        cmds = [c for c in (commands or []) if c and c.strip()]

        body = self._default_body(
            name=slug, when_to_use=when, workflow=steps, commands=cmds
        )
        if extra_sections:
            extras = [
                "## {0}\n\n{1}".format(heading.strip(), content.strip())
                for heading, content in extra_sections.items()
            ]
            body = body.rstrip() + "\n\n" + "\n\n".join(extras) + "\n"

        text = render_skill_document(
            name=slug,
            description=description,
            body=body,
            tags=list(tags or ["generated", "phase-3"]),
            commands=cmds,
            source="repo-maintainer phase 3",
        )

        errors = validate_skill_document(text)
        if errors:
            self._log(
                "generated skill '{0}' is invalid: {1}".format(slug, "; ".join(errors))
            )
            return SkillRecord(
                name=slug,
                description=description,
                origin="generated",
                valid=False,
                errors=errors,
            )

        staged = self.stage_skill(slug, text=text, source_url=None)
        self._log("generated and staged skill '{0}'".format(slug))
        return SkillRecord(
            name=slug,
            description=description,
            path=staged.path,
            origin="generated",
            version=staged.version,
            tags=staged.tags or list(tags or ["generated", "phase-3"]),
            valid=True,
        )

    def ensure_coverage(
        self,
        domain: str,
        *,
        summary: str = "",
        commands: Optional[Sequence[str]] = None,
        search: bool = True,
    ) -> SkillRecord:
        """Ensure a skill exists for ``domain``.

        Order of operations: local staged coverage, then the registry, then
        generate a new skill. Always returns a record; the caller inspects
        ``valid`` and ``origin``.
        """
        if self.has_coverage(domain):
            self._log("local staged skill already covers '{0}'".format(domain))
            for record in self.validate_all():
                if self.slug_to_name(domain) in record.name:
                    return record

        if search:
            for found in self.search(domain, limit=3):
                if self._is_relevant(found, domain):
                    self._log(
                        "staging discovered skill '{0}' for '{1}'".format(
                            found.name, domain
                        )
                    )
                    return self.stage_discovered(found)

        self._log("no skill found for '{0}'; generating a new one".format(domain))
        return self.generate_skill(domain=domain, summary=summary, commands=commands)

    @staticmethod
    def _is_relevant(record: SkillRecord, domain: str) -> bool:
        tokens = {tok for tok in re.split(r"[^a-z0-9]+", domain.lower()) if len(tok) > 2}
        if not tokens:
            return True
        haystack = "{0} {1}".format(record.name, record.description).lower()
        return any(tok in haystack for tok in tokens)

    # -- inspection / validation ------------------------------------------- #

    def load_skill(self, path: Path) -> SkillRecord:
        """Read and validate a SKILL.md from disk (path may be file or dir)."""
        skill_file = Path(path)
        if skill_file.is_dir():
            skill_file = skill_file / SKILL_FILENAME
        record = SkillRecord(
            name=skill_file.parent.name or skill_file.stem, path=skill_file
        )
        if not skill_file.exists():
            record.errors = ["file not found: {0}".format(skill_file)]
            return record
        text = skill_file.read_text(encoding="utf-8", errors="replace")
        record.errors = validate_skill_document(text)
        record.valid = not record.errors
        try:
            meta, _body = parse_frontmatter(text)
        except ValueError:
            meta = {}
        record.name = str(meta.get("name") or record.name)
        record.description = str(meta.get("description") or "")
        record.version = str(meta.get("version") or "0.1.0")
        tags = meta.get("tags")
        record.tags = [str(tag) for tag in tags] if isinstance(tags, list) else []
        return record

    def validate_all(self) -> List[SkillRecord]:
        """Validate every staged skill, sorted by directory name."""
        if not self.staging_dir.exists():
            return []
        found: List[SkillRecord] = []
        for child in sorted(self.staging_dir.iterdir()):
            if child.is_dir() and (child / SKILL_FILENAME).exists():
                found.append(self.load_skill(child))
        return found

    def index(self) -> List[SkillRecord]:
        """Alias for :meth:`validate_all` -- staged skills and their state."""
        return self.validate_all()

    def has_coverage(self, term: str) -> bool:
        """Heuristic: does a staged skill already cover ``term``?"""
        needle = self.slug_to_name(term)
        tokens = {tok for tok in re.split(r"[^a-z0-9]+", needle) if len(tok) > 2}
        if not tokens:
            return False
        for record in self.validate_all():
            haystack = " ".join(
                [record.name, record.description, " ".join(record.tags)]
            ).lower()
            if needle in haystack or all(tok in haystack for tok in tokens):
                return True
        return False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skill_manager",
        description="Discover, stage, generate, and validate agent skills.",
    )
    parser.add_argument(
        "--staging-dir",
        type=Path,
        default=None,
        help="Sandbox directory (default: ~/.cline/skills/staged).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report actions without writing files."
    )
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="Search the Open Skills registry.")
    p_search.add_argument("term", help="Search term.")
    p_search.add_argument("--limit", type=int, default=5)

    p_stage = sub.add_parser("stage", help="Stage a discovered skill stub.")
    p_stage.add_argument("skill", help="owner/repo slug.")

    p_gen = sub.add_parser("generate", help="Generate a new SKILL.md.")
    p_gen.add_argument("domain", help="Uncovered tool or domain.")
    p_gen.add_argument("--summary", default="", help="One-paragraph description.")
    p_gen.add_argument("--command", action="append", default=[], dest="commands")

    sub.add_parser("list", help="List staged skills and validation state.")
    p_val = sub.add_parser("validate", help="Validate one staged skill or all.")
    p_val.add_argument("name", nargs="?", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``python3 skill_manager.py <command>``."""
    args = _build_parser().parse_args(argv)
    manager = SkillManager(staging_dir=args.staging_dir, dry_run=args.dry_run)
    command = args.command or "list"

    if command == "search":
        results = manager.search(args.term, limit=args.limit)
        print(json.dumps([r.to_dict() for r in results], indent=2))
        return 0 if results else 1

    if command == "stage":
        slug = args.skill
        discovered = SkillRecord(
            name=slug,
            description=(
                "Discovered skill '{0}' from the Open Skills registry. Review "
                "and complete the workflow below before activation.".format(slug)
            ),
            origin="discovered",
            source_url="https://github.com/{0}".format(slug),
        )
        staged = manager.stage_discovered(discovered)
        print(json.dumps(staged.to_dict(), indent=2))
        return 0 if staged.valid else 1

    if command == "generate":
        record = manager.generate_skill(
            domain=args.domain, summary=args.summary, commands=args.commands
        )
        print(json.dumps(record.to_dict(), indent=2))
        return 0 if record.valid else 1

    if command == "list":
        print(
            json.dumps(
                {
                    "staging_dir": str(manager.staging_dir),
                    "exists": manager.staging_dir.exists(),
                    "skills": [r.to_dict() for r in manager.index()],
                },
                indent=2,
            )
        )
        return 0

    if command == "validate":
        if args.name:
            records = [manager.load_skill(Path(args.name))]
        else:
            records = manager.validate_all()
        print(json.dumps([r.to_dict() for r in records], indent=2))
        return 1 if any(not r.valid for r in records) else 0

    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
