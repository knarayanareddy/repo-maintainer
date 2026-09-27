"""Curator recipe for ``knarayanareddy/toolscour``.

toolscour is a 3D spatial explorer over 11,000+ open-source AI tools, models,
runtimes, agents, and skill packs. The daily objective is to ingest newly
discovered entries and re-shard so the spatial index stays fresh and fast.

The repository owns that logic in ``pipeline/harvest_ai_tools.py`` (multi-source
harvest: GitHub GraphQL plus the Hugging Face Hub) and
``pipeline/shard_builder.py`` (two-tier domain sharding). This recipe runs
those two scripts in order and only fills the gap itself when they cannot run,
so the data contract the explorer expects is never re-invented here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    CheckReport,
    CurationRecipe,
    CurationResult,
    collapse_ws,
    github_token,
    run,
    truncate,
    utc_date,
)

__all__ = ["ToolscourRecipe"]

#: Harvested corpus index, relative to the repository root.
CATALOG_PATH = "web/public/repos.json"

#: Pipeline entry points, in execution order.
HARVEST_SCRIPT = "pipeline/harvest_ai_tools.py"
SHARD_SCRIPT = "pipeline/shard_builder.py"

#: Where ``shard_builder.py`` writes its two-tier index.
SHARD_ROOT = "web/public"

#: Fallback discovery facets (AI-specific, unlike gitscour's).
FALLBACK_QUERIES = (
    "topic:llm stars:>=500",
    "topic:ai-agents stars:>=500",
    "topic:llmops stars:>=500",
    "topic:stable-diffusion stars:>=500",
    "topic:inference stars:>=500",
    "topic:vector-database stars:>=500",
    "topic:prompt-engineering stars:>=500",
    "topic:fine-tuning stars:>=500",
)


def normalise_ai_tool(item: Dict[str, Any], min_stars: int) -> Optional[Dict[str, Any]]:
    """Normalise a GitHub search result into an AI-tool corpus record.

    The star floor mirrors ``normalize_and_dedupe`` in the repository's own
    harvester, and the shape matches what it persists to ``repos.json``.
    """
    if not isinstance(item, dict):
        return None
    stars = int(item.get("stargazers_count") or 0)
    if stars < min_stars:
        return None
    owner = item.get("owner") or {}
    name = str(item.get("name") or "")
    login = str(owner.get("login") or str(item.get("full_name") or "").split("/")[0] or "")
    identifier = item.get("id")
    if not name or not login or identifier is None:
        return None
    license_info = item.get("license") or {}
    topics = list(item.get("topics") or [])
    return {
        "id": identifier,
        "name": name,
        "owner": login,
        "full_name": str(item.get("full_name") or "{0}/{1}".format(login, name)),
        "description": collapse_ws(item.get("description") or ""),
        "stars": stars,
        "forks": int(item.get("forks_count") or 0),
        "language": str(item.get("language") or "Other"),
        "license": str(license_info.get("spdx_id") or license_info.get("name") or "Unknown"),
        "topics": topics,
        "pushed_at": str(item.get("pushed_at") or ""),
        "homepage": str(item.get("homepage") or ""),
        "archived": bool(item.get("archived") or False),
    }


# --------------------------------------------------------------------------- #
# Recipe
# --------------------------------------------------------------------------- #


class ToolscourRecipe(CurationRecipe):
    """Ingest newly discovered AI tools and re-shard the 3D spatial index."""

    recipe_id = "toolscour"
    title = "toolscour AI tool ingest and re-shard"
    summary = (
        "Run the repository's harvest_ai_tools.py and shard_builder.py to ingest "
        "newly discovered tools, models, runtimes and agents, then re-shard the "
        "two-tier spatial index (REST discovery fallback when the pipeline is "
        "unavailable)."
    )

    # -- configuration ------------------------------------------------------ #

    def min_stars(self) -> int:
        """Star floor, matching the repository's own 500-star gate."""
        return max(1, self.option("min_stars", 500))

    def max_new(self) -> int:
        """Upper bound on newly ingested tools per run."""
        return max(1, min(5000, self.option("max_new", 300)))

    def harvest_pages(self) -> int:
        """GraphQL pages per query handed to the repository's harvester."""
        return max(1, min(10, self.option("harvest_pages", 3)))

    def include_hf(self) -> bool:
        """Whether to include the Hugging Face Hub pass."""
        return bool(self.option("include_hf", True))

    def pipeline_timeout(self) -> int:
        """Seconds allowed for each pipeline stage."""
        return max(30, self.option("pipeline_timeout", 900))

    # -- preflight ---------------------------------------------------------- #

    def check(self, workspace_path: Path) -> CheckReport:
        """Confirm the ingest and sharding scripts are present and runnable."""
        root = Path(workspace_path)
        report = CheckReport(recipe=self.recipe_id)
        report.add("clone", root.is_dir(), str(root))
        report.require(root / "pipeline", "pipeline-dir", relative_to=root)
        for relative, label, fatal in (
            (HARVEST_SCRIPT, "harvester", True),
            (SHARD_SCRIPT, "shard-builder", False),
            (CATALOG_PATH, "catalog", False),
        ):
            path = root / relative
            report.add(
                label,
                path.is_file(),
                "{0} {1}".format(
                    relative,
                    "present" if path.is_file() else "missing",
                ),
                fatal=fatal,
            )
        token = github_token()
        report.add(
            "token",
            bool(token),
            "GitHub token resolved" if token
            else "no GitHub token: harvest will use unauthenticated limits",
            fatal=False,
        )
        report.add(
            "python",
            True,
            "pipeline will run with {0}".format(self.python_executable()),
            fatal=False,
        )
        return report

    # -- pipeline execution ------------------------------------------------- #

    def run_harvester(self, root: Path) -> Tuple[bool, str]:
        """Run ``pipeline/harvest_ai_tools.py`` with the configured knobs."""
        script = root / HARVEST_SCRIPT
        if not script.is_file():
            return False, "harvester missing"
        args = [
            self.python_executable(),
            HARVEST_SCRIPT,
            "--pages",
            str(self.harvest_pages()),
            "--output",
            CATALOG_PATH,
        ]
        if not self.include_hf():
            args.append("--no-hf")
        code, out, err = run(
            args, cwd=root, timeout=self.pipeline_timeout(), env=self.command_env()
        )
        detail = collapse_ws((out or "")[-400:] or (err or "")[-400:])
        if code == 0:
            self.log("harvester finished: {0}".format(truncate(detail, 180) or "no output"))
            return True, detail
        self.log("harvester failed ({0}): {1}".format(code, truncate(detail, 180)))
        return False, detail

    def run_shard_builder(self, root: Path) -> Tuple[bool, str]:
        """Run ``pipeline/shard_builder.py`` to refresh the two-tier index."""
        script = root / SHARD_SCRIPT
        if not script.is_file():
            return False, "shard builder missing"
        # shard_builder.py writes under <out>/data/details and expects the
        # directories to exist, so create them before invoking it.
        (root / SHARD_ROOT / "data" / "details").mkdir(parents=True, exist_ok=True)
        code, out, err = run(
            [
                self.python_executable(),
                SHARD_SCRIPT,
                "--input",
                CATALOG_PATH,
                "--out",
                SHARD_ROOT,
            ],
            cwd=root,
            timeout=self.pipeline_timeout(),
        )
        detail = collapse_ws((out or "")[-400:] or (err or "")[-400:])
        if code == 0:
            self.log("shard builder finished: {0}".format(truncate(detail, 180) or "no output"))
            return True, detail
        self.log("shard builder failed ({0}): {1}".format(code, truncate(detail, 180)))
        return False, detail

    # -- fallback ingest ---------------------------------------------------- #

    def discover_tools(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Discover AI tools through the REST search API."""
        floor = self.min_stars()
        since = ">{0}".format(utc_date())
        seen: set = set()
        tools: List[Dict[str, Any]] = []
        notes: List[str] = []
        for query in FALLBACK_QUERIES:
            if len(tools) >= self.max_new():
                break
            full = "{0} stars:>={1} created:{2}".format(query, floor, since)
            items, error = self.github_search(full, limit=50, sort="stars")
            if error:
                notes.append("search '{0}' failed: {1}".format(query, truncate(error, 80)))
                continue
            added = 0
            for item in items:
                record = normalise_ai_tool(item, floor)
                if not record:
                    continue
                key = str(record["full_name"]).lower()
                if key in seen:
                    continue
                seen.add(key)
                tools.append(record)
                added += 1
                if len(tools) >= self.max_new():
                    break
            notes.append("query '{0}' contributed {1} tool(s)".format(query, added))
        return tools, notes

    @staticmethod
    def corpus_key(row: Dict[str, Any]) -> str:
        """Stable identity for a corpus row."""
        if row.get("id") is not None:
            return "id:{0}".format(row["id"])
        return "slug:{0}".format(str(row.get("full_name") or "").lower())

    def merge_corpus(
        self, plan, existing: List[Dict[str, Any]], fresh: List[Dict[str, Any]]
    ) -> Tuple[int, int]:
        """Merge ``fresh`` into ``existing``; returns ``(added, updated)``."""
        by_key = {self.corpus_key(row): row for row in existing}
        added = updated = 0
        for record in fresh:
            key = self.corpus_key(record)
            current = by_key.get(key)
            if current is None:
                by_key[key] = record
                added += 1
            elif int(record.get("stars") or 0) > int(current.get("stars") or 0):
                merged = dict(current)
                for field in (
                    "stars", "forks", "description", "language", "license",
                    "topics", "pushed_at", "homepage", "archived",
                ):
                    merged[field] = record.get(field, current.get(field))
                by_key[key] = merged
                updated += 1
        rows = sorted(
            by_key.values(), key=lambda row: int(row.get("stars") or 0), reverse=True
        )
        plan.write_json(CATALOG_PATH, rows)
        return added, updated

    def load_corpus(self, plan) -> List[Dict[str, Any]]:
        """Read the existing corpus rows."""
        data = plan.read_json(CATALOG_PATH, default=[])
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    def summarise(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Headline statistics for the PR body and verify step."""
        stars = [int(row.get("stars") or 0) for row in rows]
        return {
            "rows": len(rows),
            "min_stars_seen": min(stars) if stars else 0,
            "max_stars_seen": max(stars) if stars else 0,
            "total_stars": sum(stars),
            "unique_keys": len({self.corpus_key(row) for row in rows}),
        }

    # -- production --------------------------------------------------------- #

    def curate(self, workspace_path: Path, dry_run: bool = False) -> CurationResult:
        """Ingest new tools and re-shard the spatial index."""
        root = Path(workspace_path)
        result = CurationResult(recipe=self.recipe_id, dry_run=bool(dry_run))
        self.result = result
        plan = self.new_plan(root, dry_run=dry_run)

        before = self.summarise(self.load_corpus(plan))
        result.note(
            "corpus before: {rows} tools, {total_stars} stars tracked".format(**before)
        )

        harvested, detail = self.run_harvester(root)
        if harvested:
            result.note("ingested with the repository's own harvester")
            if not dry_run:
                result.add(
                    "corpus",
                    CATALOG_PATH,
                    path=CATALOG_PATH,
                    detail="regenerated by pipeline/harvest_ai_tools.py",
                )
        else:
            result.note(
                "harvester unavailable ({0}); using REST discovery".format(
                    truncate(detail, 120)
                )
            )
            fresh, notes = self.discover_tools()
            for note in notes[:8]:
                result.note(note)
            if not fresh:
                result.note(
                    "no new AI tools cleared {0} stars; nothing to ingest".format(
                        self.min_stars()
                    )
                )
                return self.finish(result, plan)
            added, updated = self.merge_corpus(plan, self.load_corpus(plan), fresh)
            result.add(
                "corpus",
                CATALOG_PATH,
                path=CATALOG_PATH,
                detail="{0} new tool(s), {1} refreshed, {2} discovered".format(
                    added, updated, len(fresh)
                ),
            )

        if dry_run:
            result.note("[dry-run] the shard builder was not invoked")
            return self.finish(result, plan)

        sharded, shard_detail = self.run_shard_builder(root)
        if sharded:
            result.add(
                "shards",
                "two-tier shard index",
                path="{0}/data/details/".format(SHARD_ROOT),
                detail=truncate(shard_detail, 160) or "rebuilt by pipeline/shard_builder.py",
            )
        else:
            result.problems.append(
                "shard builder did not complete: {0}".format(truncate(shard_detail, 200))
            )

        after = self.summarise(self.load_corpus(plan))
        result.note(
            "corpus after: {rows} tools (+{delta}), {unique_keys} unique".format(
                delta=after["rows"] - before["rows"], **after
            )
        )
        if after["rows"] < before["rows"]:
            result.problems.append(
                "corpus shrank from {0} to {1} rows".format(before["rows"], after["rows"])
            )
        return self.finish(result, plan)

    # -- postconditions ----------------------------------------------------- #

    def verify(self, workspace_path: Path) -> List[str]:
        """Check the corpus parses, respects the star floor, and is sharded."""
        root = Path(workspace_path)
        problems: List[str] = []
        plan = self.new_plan(root, dry_run=True)
        data = plan.read_json(CATALOG_PATH, default=None)
        if data is None:
            return ["{0} is missing or is not valid JSON".format(CATALOG_PATH)]
        if not isinstance(data, list):
            return ["{0} must contain a JSON array".format(CATALOG_PATH)]
        if not data:
            return ["{0} is empty".format(CATALOG_PATH)]

        floor = self.min_stars()
        keys: set = set()
        below = 0
        for row in data:
            if not isinstance(row, dict):
                problems.append("{0}: contains a non-object row".format(CATALOG_PATH))
                break
            key = self.corpus_key(row)
            if key in keys:
                problems.append("{0}: duplicate record {1}".format(CATALOG_PATH, key))
            keys.add(key)
            if int(row.get("stars") or 0) < floor:
                below += 1
        if below:
            problems.append(
                "{0}: {1} row(s) fall below the {2}-star floor".format(
                    CATALOG_PATH, below, floor
                )
            )

        details = root / SHARD_ROOT / "data" / "details"
        if details.is_dir():
            shards = sorted(details.glob("*.json"))
            if not shards:
                problems.append(
                    "{0} exists but holds no shards; run pipeline/shard_builder.py".format(
                        details.relative_to(root)
                    )
                )
            for shard in shards:
                try:
                    json.loads(shard.read_text(encoding="utf-8"))
                except (OSError, ValueError, UnicodeDecodeError) as exc:
                    problems.append(
                        "shard {0} is not valid JSON: {1}".format(
                            shard.relative_to(root), exc
                        )
                    )
                    break
        else:
            problems.append(
                "shard directory {0} is missing; the 3D index would be empty".format(
                    (root / SHARD_ROOT / "data" / "details").relative_to(root)
                )
            )
        return problems
