"""Curator recipe for ``knarayanareddy/AI-Arsenal``.

AI-Arsenal is a machine-readable encyclopedia with a strict quality gate: every
entry is Markdown with YAML frontmatter validated against
``schemas/*.schema.json``, and every controlled-vocabulary value must come from
``TAXONOMY.md``. Writing a plausible-looking entry that fails
``node scripts/validate-schema.js`` is worse than writing nothing, so this
recipe refuses to guess:

1. read the governance files (``AGENT.md``, ``CONTEXT.md``, ``TAXONOMY.md``) and
   extract the actual controlled vocabulary and quality rules;
2. read the ``required`` field list straight out of ``schemas/tool.schema.json``
   so the frontmatter contract is never hard-coded and never drifts;
3. discover genuinely new tools via the GitHub search API and drop anything
   already present in ``content/``;
4. have Gemini draft each entry, then sanitise it: drop unknown enum values,
   clamp the description to the schema's 160-character limit, and force the
   bookkeeping fields (``added_date``, ``last_reviewed``, ``added_by``);
5. verify with the repository's own validator when ``node`` is available, and
   with a local frontmatter contract check when it is not.

Only new files under ``content/`` are ever touched — no unrelated file, index,
or generated artefact is modified.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .base import (
    CheckReport,
    CurationRecipe,
    CurationResult,
    collapse_ws,
    hours_ago_iso,
    iso_utc_now,
    slugify,
    truncate,
    utc_date,
)

__all__ = ["AiArsenalRecipe"]

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*\n?", re.DOTALL)
_BULLETED_RULE_RE = re.compile(r"^[-*]\s+(?P<text>.+)$", re.MULTILINE)

#: Where canonical tool entries live, relative to the repository root.
CONTENT_GLOB = "content/tools"

#: Identity recorded for entries this pipeline authors.
CURATOR_ID = "repo-maintainer"

#: Search topics used to find new AI engineering tools.
DEFAULT_TOPIC_QUERIES = (
    "topic:llm topic:agents",
    "topic:llm topic:inference",
    "topic:rag topic:vector-database",
    "topic:llmops",
    "topic:ai-agents",
)


# --------------------------------------------------------------------------- #
# Frontmatter helpers (deliberately tiny and schema-agnostic)
# --------------------------------------------------------------------------- #


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Split a Markdown document into ``(frontmatter, body)``.

    Only the subset AI-Arsenal actually uses is supported: ``key: value``,
    inline ``[a, b]`` lists, and ``- item`` block lists. Anything else is
    returned as a raw string so the caller can decide what to do with it.
    """
    match = _FRONT_MATTER_RE.match(text or "")
    if not match:
        return {}, text or ""
    data: Dict[str, Any] = {}
    key = ""
    for line in match.group("body").splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and key:
            current = data.get(key)
            if isinstance(current, list):
                current.append(_coerce(stripped[2:].strip()))
            else:
                data[key] = [_coerce(stripped[2:].strip())]
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            data[key] = [
                _coerce(part.strip()) for part in value[1:-1].split(",") if part.strip()
            ]
        else:
            data[key] = _coerce(value)
    return data, (text or "")[match.end():]


def _coerce(value: str) -> Any:
    """Coerce a scalar frontmatter value to a Python type."""
    raw = str(value or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("null", "~", "none"):
        return None
    return raw


def render_frontmatter(fields: Dict[str, Any]) -> str:
    """Render a frontmatter block, preserving the declared key order."""
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            if not value:
                lines.append("{0}: []".format(key))
            elif all(isinstance(item, (int, float, bool)) or str(item).isalnum()
                     for item in value) and len(str(value)) < 60:
                lines.append(
                    "{0}: [{1}]".format(key, ", ".join(str(item) for item in value))
                )
            else:
                lines.append("{0}:".format(key))
                lines.extend("  - {0}".format(item) for item in value)
        elif value is None:
            lines.append("{0}: null".format(key))
        else:
            lines.append("{0}: {1}".format(key, value))
    lines.append("---")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Recipe
# --------------------------------------------------------------------------- #


class AiArsenalRecipe(CurationRecipe):
    """Add new tool entries that satisfy the repository's schema and taxonomy."""

    recipe_id = "ai-arsenal"
    title = "AI-Arsenal knowledge entry expansion"
    summary = (
        "Parse AGENT.md, CONTEXT.md and TAXONOMY.md for the quality gate, discover "
        "newly released AI tools, and emit schema-valid content/tools entries "
        "without touching any unrelated file."
    )

    # -- configuration ------------------------------------------------------ #

    def items_per_run(self) -> int:
        """How many new entries to author today."""
        return max(1, min(25, self.option("items_per_run", 3)))

    def min_stars(self) -> int:
        """Star floor a candidate must clear to be worth an entry."""
        return max(0, self.option("min_stars", 800))

    def topic_queries(self) -> List[str]:
        """GitHub search queries used to surface new tools."""
        configured = self.option("topic_queries", "")
        if configured:
            return [item.strip() for item in str(configured).split(",") if item.strip()]
        return list(DEFAULT_TOPIC_QUERIES)

    # -- preflight ---------------------------------------------------------- #

    def check(self, workspace_path: Path) -> CheckReport:
        """Confirm the governance files, schema, and validator are all present."""
        root = Path(workspace_path)
        report = CheckReport(recipe=self.recipe_id)
        report.add("clone", root.is_dir(), str(root))
        for relative, label in (
            ("AGENT.md", "agent-md"),
            ("CONTEXT.md", "context-md"),
            ("TAXONOMY.md", "taxonomy"),
            ("schemas/tool.schema.json", "tool-schema"),
            (CONTENT_GLOB, "content-dir"),
        ):
            report.require(root / relative, label, relative_to=root)

        schema = self.load_schema(root)
        required = list((schema.get("required") or [])) if isinstance(schema, dict) else []
        report.add(
            "schema-required",
            bool(required),
            "{0} required frontmatter field(s): {1}".format(
                len(required), ", ".join(required[:8]) or "(none)"
            ),
        )
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        max_length = 0
        if isinstance(properties, dict):
            desc = properties.get("description") or {}
            if isinstance(desc, dict):
                max_length = int(desc.get("maxLength") or 0)
        self._max_description = max_length or 160
        report.add(
            "description-limit",
            self._max_description > 0,
            "description clamped to {0} characters".format(self._max_description),
            fatal=False,
        )

        node = self.node_executable()
        validator = root / "scripts" / "validate-schema.js"
        report.add(
            "node",
            bool(node),
            "node {0} available for the repository's own validator".format(node)
            if node
            else "node not found: falling back to a local frontmatter check",
            fatal=False,
        )
        report.add(
            "validator",
            validator.is_file(),
            "scripts/validate-schema.js present" if validator.is_file()
            else "repository validator not found; local contract check will be used",
            fatal=False,
        )
        report.add(
            "llm",
            self.llm_enabled,
            "Gemini synthesis enabled"
            if self.llm_enabled
            else "offline mode: no new entries can be authored without synthesis",
            fatal=False,
        )
        return report

    # -- repository intelligence -------------------------------------------- #

    def load_schema(self, root: Path) -> Dict[str, Any]:
        """Load ``schemas/tool.schema.json`` (empty dict when unavailable)."""
        path = Path(root) / "schemas" / "tool.schema.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def quality_rules(self, root: Path, limit: int = 8) -> List[str]:
        """Extract the normative rules the repository states about itself."""
        rules: List[str] = []
        for name in ("AGENT.md", "CONTEXT.md"):
            path = Path(root) / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in _BULLETED_RULE_RE.finditer(text):
                line = collapse_ws(match.group("text"))
                lowered = line.lower()
                if any(
                    token in lowered
                    for token in ("must", "never", "always", "required", "do not", "don't")
                ):
                    rules.append("- " + truncate(line, 160))
                if len(rules) >= limit:
                    return rules
        return rules

    def taxonomy(self, root: Path) -> Dict[str, List[str]]:
        """Read the controlled vocabulary out of ``TAXONOMY.md``.

        Each ``## Heading`` section that lists bullets becomes one controlled
        vocabulary keyed by the heading's last path segment.
        """
        path = Path(root) / "TAXONOMY.md"
        vocab: Dict[str, List[str]] = {}
        if not path.is_file():
            return vocab
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - defensive
            return vocab
        key = ""
        for line in text.splitlines():
            heading = line.strip()
            if heading.startswith("## "):
                key = slugify(heading[3:], fallback="misc")
                vocab.setdefault(key, [])
                continue
            bullet = _BULLETED_RULE_RE.match(heading)
            if bullet and key:
                value = collapse_ws(bullet.group("text")).split(" —")[0].strip()
                value = value.strip("`").split("`")[0].strip()
                if value and len(value) < 60:
                    vocab[key].append(value)
        return {key: sorted(set(values)) for key, values in vocab.items() if values}

    def existing_entries(self, root: Path) -> Tuple[Set[str], Dict[str, str]]:
        """Return ``(known ids, github url -> id)`` for everything on disk."""
        ids: Set[str] = set()
        urls: Dict[str, str] = {}
        base = Path(root) / CONTENT_GLOB
        for path in self.walk_files(base, ".md", limit=3000):
            try:
                head = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - defensive
                continue
            data, _body = parse_frontmatter(head)
            ident = str(data.get("id") or "").strip()
            if ident:
                ids.add(ident.lower())
            for key in ("github_url", "url", "source_url"):
                value = str(data.get(key) or "").strip().lower()
                if value.startswith("https://github.com/"):
                    urls[value.rstrip("/")] = ident or path.stem
        return ids, urls

    # -- candidate discovery ------------------------------------------------ #

    def discover_candidates(self, root: Path, limit: int) -> List[Dict[str, Any]]:
        """Find new, notable AI tools that are not already in ``content/``."""
        known_ids, known_urls = self.existing_entries(root)
        floor = self.min_stars()
        since = hours_ago_iso(max(24, self.option("window_hours", 24 * 30)))
        seen: Set[str] = set()
        candidates: List[Dict[str, Any]] = []
        for query in self.topic_queries():
            if len(candidates) >= limit * 3:
                break
            full = "{0} stars:>={1} pushed:>{2}".format(query, floor, since)
            items, error = self.github_search(full, limit=25, sort="updated")
            if error:
                self.log("search failed for '{0}': {1}".format(query, truncate(error, 90)))
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("html_url") or "").rstrip("/").lower()
                if not url or url in seen or url in known_urls:
                    continue
                if str(item.get("description") or "").strip() == "":
                    continue
                seen.add(url)
                candidates.append(item)
        candidates.sort(key=lambda item: int(item.get("stargazers_count") or 0), reverse=True)
        return [
            item for item in candidates
            if slugify(str((item.get("name") or ""))) not in known_ids
        ][: max(limit * 3, limit)]

    # -- entry synthesis ---------------------------------------------------- #

    def _schema_prompt(self, schema: Dict[str, Any], vocab: Dict[str, List[str]]) -> str:
        """Describe the required frontmatter contract to the model."""
        required = list(schema.get("required") or []) if isinstance(schema, dict) else []
        lines = ["Required frontmatter keys (all must be present): {0}".format(
            ", ".join(required) or "(read schemas/tool.schema.json yourself)"
        )]
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        if isinstance(properties, dict):
            for key in required:
                spec = properties.get(key)
                if not isinstance(spec, dict):
                    continue
                hint = str(spec.get("description") or "").strip()
                allowed = spec.get("enum")
                bits = [bit for bit in (hint, ("enum: " + ", ".join(map(str, allowed))) if allowed else "") if bit]
                if bits:
                    lines.append("  {0}: {1}".format(key, truncate(" | ".join(bits), 200)))
        if vocab:
            lines.append("Controlled vocabulary from TAXONOMY.md (use these values verbatim):")
            for key, values in sorted(vocab.items())[:12]:
                lines.append("  {0}: {1}".format(key, ", ".join(values[:14])))
        return "\n".join(lines)

    def _draft_entry(
        self, candidate: Dict[str, Any], schema: Dict[str, Any], vocab: Dict[str, List[str]],
        rules: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Ask the model for one entry, then sanitise it against the contract."""
        contract = self._schema_prompt(schema, vocab)
        prompt = (
            "Write one canonical, highly detailed AI-Arsenal tool entry for this GitHub project.\n\n"
            "Project: {name}\n"
            "Owner: {owner}\n"
            "URL: {url}\n"
            "Description: {description}\n"
            "Stars: {stars}\n"
            "Primary language: {language}\n"
            "License: {license}\n"
            "Topics: {topics}\n"
            "Last push: {pushed}\n\n"
            "Quality gate for this repository:\n{rules}\n\n"
            "Frontmatter contract:\n{contract}\n\n"
            "Requirements for high quality encyclopedia entries:\n"
            "1. Overview: 2-3 substantive paragraphs explaining the architectural foundations, technical mechanics, and core purpose.\n"
            "2. Why It's in the Arsenal: Specific, concrete technical differentiators, design advantages, and where it excels over alternative stacks.\n"
            "3. Key Features: 4-6 specific technical capabilities (with real command flags, API primitives, or protocol details where applicable).\n"
            "4. Trade-offs: Honest engineering constraints (memory footprint, dependency graph, scaling bottlenecks, operational complexities).\n"
            "5. Tone: Technical, rigorous, zero marketing fluff, zero placeholder phrases.\n\n"
            "Return JSON only, an object with exactly two keys:\n"
            '  "frontmatter" - an object holding every required key, using only '
            "controlled-vocabulary values where the contract lists them\n"
            '  "sections"     - an object of Markdown section name -> array of '
            "paragraph strings; must include Overview, Why It\'s in the Arsenal, "
            "Key Features, and Trade-offs\n\n"
            "Be specific, deeply technical, and honest. Never invent false benchmarks or fake stats."
        ).format(
            name=candidate.get("full_name") or candidate.get("name"),
            owner=(candidate.get("owner") or {}).get("login", ""),
            url=candidate.get("html_url") or "",
            description=truncate(candidate.get("description") or "", 300),
            stars=candidate.get("stargazers_count") or 0,
            language=candidate.get("language") or "Unknown",
            license=((candidate.get("license") or {}) or {}).get("spdx_id") or "Unknown",
            topics=", ".join(candidate.get("topics") or []) or "(none)",
            pushed=candidate.get("pushed_at") or "unknown",
            rules="\n".join(rules) or "- (none published)",
            contract=contract,
        )
        payload = self.llm.complete_json(
            prompt,
            system=(
                "You are a meticulous technical editor maintaining a machine-readable "
                "AI engineering encyclopedia. Correctness and schema compliance matter "
                "more than prose."
            ),
            max_output_tokens=6144,
            temperature=0.5,
        )
        if not isinstance(payload, dict):
            return None
        front = payload.get("frontmatter")
        sections = payload.get("sections")
        if not isinstance(front, dict) or not isinstance(sections, dict):
            return None
        return self._sanitize_entry(front, sections, candidate, schema, vocab)

    def _sanitize_entry(
        self,
        front: Dict[str, Any],
        sections: Dict[str, Any],
        candidate: Dict[str, Any],
        schema: Dict[str, Any],
        vocab: Dict[str, List[str]],
    ) -> Dict[str, Any]:
        """Force the draft into the shape the schema and taxonomy demand."""
        required = list(schema.get("required") or []) if isinstance(schema, dict) else []
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        name = collapse_ws(front.get("name")) or str(
            candidate.get("full_name") or candidate.get("name") or "unknown"
        )
        repo_name = str(candidate.get("name") or name)
        today = utc_date()
        limit = getattr(self, "_max_description", 160) or 160

        fields: Dict[str, Any] = {}
        for key in required or sorted(front):
            value = front.get(key)
            spec = properties.get(key) if isinstance(properties, dict) else None
            allowed = spec.get("enum") if isinstance(spec, dict) else None
            if isinstance(allowed, list) and value not in allowed:
                value = self._nearest_vocab(str(value or ""), allowed)
            fields[key] = value

        fields["id"] = slugify(str(fields.get("id") or repo_name), max_length=60)
        fields["name"] = name
        fields["type"] = "tool"
        fields["description"] = truncate(
            fields.get("description") or candidate.get("description") or "", limit
        )
        fields["url"] = str(fields.get("url") or candidate.get("html_url") or "")
        fields["github_url"] = str(fields.get("github_url") or candidate.get("html_url") or "")
        fields.setdefault("docs_url", None)
        fields["added_date"] = today
        fields["last_reviewed"] = today
        fields["added_by"] = CURATOR_ID
        for key in ("alternatives", "integrates_with", "tags", "job", "audience"):
            if key in fields and not isinstance(fields[key], list):
                value = fields[key]
                fields[key] = [value] if value else []
        if isinstance(fields.get("tags"), list):
            fields["tags"] = [self._nearest_vocab(str(t), vocab.get("tags", [])) or str(t).lower()
                              for t in fields["tags"]][:8]
        phase = str(fields.get("phase") or "")
        if phase and vocab.get("phase") and phase not in vocab["phase"]:
            fields["phase"] = self._nearest_vocab(phase, vocab["phase"]) or phase
        return {"frontmatter": fields, "sections": sections}

    @staticmethod
    def _nearest_vocab(value: str, allowed: Sequence[str]) -> str:
        """Snap a model-supplied value onto the nearest allowed vocabulary item.

        An exact match wins; otherwise the first allowed value that contains or
        is contained by the input is used, and ``""`` means "no safe mapping",
        which tells the caller to leave the field for a human.
        """
        if not allowed:
            return value
        text = str(value or "").strip().lower()
        for item in allowed:
            if str(item).lower() == text:
                return item
        for item in allowed:
            low = str(item).lower()
            if text and (low in text or text in low):
                return item
        return ""

    def render_entry(self, draft: Dict[str, Any]) -> str:
        """Render a draft into the canonical entry Markdown document."""
        front = render_frontmatter(draft["frontmatter"])
        sections = draft.get("sections") or {}
        order = ["Overview", "Why It's in the Arsenal", "Key Features", "Trade-offs"]
        body_lines: List[str] = []
        for name in order + [key for key in sections if key not in order]:
            paragraphs = sections.get(name)
            if not paragraphs:
                continue
            body_lines.append("## {0}".format(name))
            body_lines.append("")
            if isinstance(paragraphs, str):
                paragraphs = [paragraphs]
            for paragraph in paragraphs:
                text = collapse_ws(paragraph)
                if text:
                    body_lines.append(text)
                    body_lines.append("")
        if not body_lines:
            body_lines = ["## Overview", "", "_Entry awaiting editorial review._", ""]
        return front + "\n\n" + "\n".join(body_lines).rstrip() + "\n"

    # -- production --------------------------------------------------------- #

    def curate(self, workspace_path: Path, dry_run: bool = False) -> CurationResult:
        """Author new tool entries and write them under ``content/tools``."""
        root = Path(workspace_path)
        result = CurationResult(recipe=self.recipe_id, dry_run=bool(dry_run))
        self.result = result
        plan = self.new_plan(root, dry_run=dry_run)

        schema = self.load_schema(root)
        vocab = self.taxonomy(root)
        rules = self.quality_rules(root)
        self._max_description = 160
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        if isinstance(properties, dict):
            desc = properties.get("description")
            if isinstance(desc, dict):
                self._max_description = int(desc.get("maxLength") or 160)
        result.note(
            "loaded {0} taxonomy group(s) and {1} schema field(s) from {2}".format(
                len(vocab), len(list(schema.get("required") or [])), "schemas/tool.schema.json"
            )
        )
        if rules:
            result.note("quality gate: {0} rule(s) read from AGENT.md/CONTEXT.md".format(len(rules)))

        if not self.llm_enabled or self.llm is None:
            result.problems.append(
                "no synthesis client: AI-Arsenal entries cannot be authored offline "
                "(set curator.llm=true and provide GEMINI_API_KEY)"
            )
            return result

        wanted = self.items_per_run()
        candidates = self.discover_candidates(root, wanted)
        if not candidates:
            result.note(
                "no new candidates above {0} stars that are absent from {1}".format(
                    self.min_stars(), CONTENT_GLOB
                )
            )
            return self.finish(result, plan)
        result.note("discovered {0} candidate(s) to consider".format(len(candidates)))

        written = 0
        shipped_paths: List[str] = []
        for candidate in candidates:
            if written >= wanted:
                break
            slug = slugify(str(candidate.get("name") or ""))
            relative = "{0}/{1}.md".format(CONTENT_GLOB, slug)
            if (root / relative).exists() or plan.exists(relative):
                continue
            draft = self._draft_entry(candidate, schema, vocab, rules)
            if draft is None:
                result.note("skipped '{0}': the model returned no usable entry".format(slug))
                continue
            missing = [
                key
                for key in (schema.get("required") or [])
                if key not in draft["frontmatter"] or draft["frontmatter"][key] in (None, "", [])
            ]
            if missing:
                result.problems.append(
                    "'{0}' is missing required frontmatter: {1}".format(
                        slug, ", ".join(missing[:6])
                    )
                )
                continue
            plan.write(relative, self.render_entry(draft))
            shipped_paths.append(relative)
            result.add(
                "entry",
                str(draft["frontmatter"].get("name") or slug),
                path=relative,
                detail="{0} — {1}".format(
                    draft["frontmatter"].get("phase") or "unphased",
                    truncate(draft["frontmatter"].get("description") or "", 90),
                ),
            )
            written += 1
        self._shipped_paths = shipped_paths

        if written:
            result.llm_used = True
            result.note(
                "run `pnpm run validate:changed` (or `node scripts/validate-schema.js "
                "--changed-only`) before merging"
            )
        return self.finish(result, plan)

    # -- postconditions ----------------------------------------------------- #

    def verify(self, workspace_path: Path) -> List[str]:
        """Check the frontmatter contract of every entry this recipe authored."""
        root = Path(workspace_path)
        schema = self.load_schema(root)
        required = list(schema.get("required") or []) if isinstance(schema, dict) else []
        vocab = self.taxonomy(root)
        known_ids, _urls = self.existing_entries(root)
        problems: List[str] = []
        if not required:
            problems.append("schemas/tool.schema.json could not be read; nothing verified")
            return problems

        node = self.node_executable()
        validator = root / "scripts" / "validate-schema.js"
        node_modules = root / "node_modules"
        if node and validator.is_file() and node_modules.is_dir():
            code, out, err = self._run_validator(root, node, validator)
            if code != 0:
                if "MODULE_NOT_FOUND" in (err or out) or "ERR_MODULE_NOT_FOUND" in (err or out):
                    self.log("validate-schema.js missing node_modules dependencies; using native Python schema validation")
                else:
                    problems.append(
                        "validate-schema.js failed: {0}".format(
                            truncate(err or out, 300)
                        )
                    )
                    return problems
        else:
            problems.append(
                "note: node/validate-schema.js unavailable or node_modules missing, "
                "so the native frontmatter contract was checked"
            )

        target_paths = [root / p for p in getattr(self, "_shipped_paths", [])]
        if not target_paths:
            target_paths = [
                p for p in self.walk_files(root / CONTENT_GLOB, ".md", limit=3000)
                if "/by-" not in str(p) and not p.name.startswith("_") and not p.name.startswith("index")
            ]

        for path in target_paths:
            try:
                data, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if not data:
                continue
            rel = path.relative_to(root)
            for key in required:
                if key not in data:
                    problems.append("{0}: missing required key '{1}'".format(rel, key))
            ident = str(data.get("id") or "")
            if ident and ident != path.stem:
                problems.append(
                    "{0}: id '{1}' does not match the file name".format(rel, ident)
                )
            if ident and list(known_ids).count(ident.lower()) > 1:
                problems.append("{0}: duplicate entry id '{1}'".format(rel, ident))
            description = str(data.get("description") or "")
            cap = int(
                ((schema.get("properties") or {}).get("description") or {}).get("maxLength")
                or 160
            )
            if description and len(description) > cap:
                problems.append(
                    "{0}: description is {1} chars (max {2})".format(
                        rel, len(description), cap
                    )
                )
            for key, allowed in sorted(vocab.items()):
                value = data.get(key)
                if isinstance(value, list):
                    for item in value:
                        if allowed and str(item) not in allowed:
                            problems.append(
                                "{0}: '{1}' is not in the {2} vocabulary".format(
                                    rel, item, key
                                )
                            )
                elif allowed and value and str(value) not in allowed:
                    problems.append(
                        "{0}: '{1}' is not in the {2} vocabulary".format(rel, value, key)
                    )
        for item in problems:
            if item.startswith("note:"):
                self.log(item)
        return [item for item in problems if not item.startswith("note:")]

    def _run_validator(self, root: Path, node: str, validator: Path) -> Tuple[int, str, str]:
        """Invoke the repository's own schema validator on changed files."""
        from .base import run  # local import keeps the module import graph flat

        return run(
            [node, str(validator.relative_to(root)), "--changed-only"],
            cwd=root,
            timeout=self.option("validator_timeout", 300),
        )
