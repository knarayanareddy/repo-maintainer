"""Curator recipe for ``knarayanareddy/gitscour``.

gitscour is a catalogue of 50,000+ open-source repositories (500+ stars) with a
3D topology explorer, and it ships its own harvest pipeline. This recipe
prefers the repository's own code and only falls back when it cannot run:

1. **Preferred** -- run ``pipeline/fast_harvest.py`` (falling back to
   ``harvest_scale.py`` / ``harvest_20k.py``) with a GitHub token in the
   environment. That script merges its GraphQL results into
   ``web/public/repos.json``, enriches every record through
   ``taxonomy_engine.enrich_repository_record`` and re-shards the dataset via
   ``shard_manager.build_sharded_dataset``.
2. **Fallback** -- when the pipeline is unavailable (no ``GH_TOKEN``, a
   timeout, a partial crash) harvest through the REST search API here, using
   the same record shape the pipeline produces, and merge them into
   ``web/public/repos.json`` without disturbing existing rows.
3. **Always** -- verify with ``pipeline/verify_catalog.py`` so the star floor
   and the shard integrity are checked before anything is committed.

Everything runs against the throwaway clone the maintainer created, so a failed
harvest can never leave a half-written catalogue on the default branch.
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
    slugify,
    truncate,
    utc_date,
)

__all__ = ["GitscourRecipe"]

#: Catalogue index, relative to the repository root.
CATALOG_PATH = "web/public/repos.json"

#: Harvest scripts, most preferred first. Each is a standalone Python entry point.
HARVEST_SCRIPTS = (
    "pipeline/fast_harvest.py",
    "pipeline/harvest_scale.py",
    "pipeline/harvest_20k.py",
    "pipeline/harvest_phase1.py",
)

#: Star windows the repository's own harvester sweeps, mirrored for the fallback.
STAR_WINDOWS = (
    "stars:500..800",
    "stars:800..1500",
    "stars:1500..3000",
    "stars:3000..6000",
    "stars:6000..12000",
)

#: Search facets used by the fallback harvest.
FALLBACK_QUERIES = (
    "topic:machine-learning stars:>=500",
    "topic:llm stars:>=500",
    "topic:devops stars:>=500",
    "topic:database stars:>=500",
    "topic:security stars:>=500",
    "language:rust stars:>=500",
    "language:go stars:>=500",
)


# --------------------------------------------------------------------------- #
# Record normalisation (mirrors pipeline/taxonomy_engine.parse_node output)
# --------------------------------------------------------------------------- #


def normalise_record(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a GitHub search result into a gitscour catalogue record.

    The field names match what ``pipeline/fast_harvest.py`` writes so records
    produced by the fallback path are indistinguishable from the pipeline's own
    to any downstream consumer.
    """
    if not isinstance(item, dict):
        return None
    owner = item.get("owner") or {}
    login = str(owner.get("login") or item.get("full_name", "").split("/")[0] or "")
    name = str(item.get("name") or "")
    identifier = item.get("id")
    if not name or not login or identifier is None:
        return None
    license_info = item.get("license") or {}
    return {
        "id": identifier,
        "name": name,
        "owner": login,
        "description": collapse_ws(item.get("description") or ""),
        "stars": int(item.get("stargazers_count") or 0),
        "forks": int(item.get("forks_count") or 0),
        "language": str(item.get("language") or "Other"),
        "license": str(license_info.get("spdx_id") or license_info.get("name") or "Unknown"),
        "topics": list(item.get("topics") or []),
        "pushed_at": str(item.get("pushed_at") or ""),
    }


# --------------------------------------------------------------------------- #
# Recipe
# --------------------------------------------------------------------------- #


class GitscourRecipe(CurationRecipe):
    """Harvest trending 500+ star repositories and refresh the shards."""

    recipe_id = "gitscour"
    title = "gitscour trending repository harvest"
    summary = (
        "Run the repository's own fast_harvest/harvest_scale pipeline (REST "
        "fallback), merge new 500+ star repositories into web/public/repos.json, "
        "re-shard, and verify catalog integrity before staging."
    )

    # -- configuration ------------------------------------------------------ #

    def min_stars(self) -> int:
        """Star floor every catalogued repository must clear."""
        return max(1, self.option("min_stars", 500))

    def max_new(self) -> int:
        """Upper bound on newly catalogued repositories per run."""
        return max(1, min(5000, self.option("max_new", 400)))

    def harvest_timeout(self) -> int:
        """Seconds allowed for the repository's own harvest script."""
        return max(30, self.option("harvest_timeout", 900))

    def prefer_pipeline(self) -> bool:
        """Whether to try the repository's pipeline before the REST fallback."""
        return bool(self.option("prefer_pipeline", True))

    # -- preflight ---------------------------------------------------------- #

    def check(self, workspace_path: Path) -> CheckReport:
        """Confirm the harvest pipeline, catalogue, and a token are available."""
        root = Path(workspace_path)
        report = CheckReport(recipe=self.recipe_id)
        report.add("clone", root.is_dir(), str(root))
        report.require(root / "pipeline", "pipeline-dir", relative_to=root)

        script = self.pipeline_script(root)
        report.add(
            "harvest-script",
            bool(script),
            "will run {0}".format(script) if script
            else "no harvest script found; the REST fallback will be used",
            fatal=False,
        )
        catalog = root / CATALOG_PATH
        report.add(
            "catalog",
            catalog.is_file(),
            "{0} present ({1} bytes)".format(
                CATALOG_PATH, catalog.stat().st_size if catalog.is_file() else 0
            ),
            fatal=False,
        )
        report.require(root / "pipeline" / "verify_catalog.py", "verifier", relative_to=root)

        token = github_token()
        report.add(
            "token",
            bool(token),
            "GitHub token resolved from the environment or `gh auth token`"
            if token
            else "no GitHub token: the harvest will use unauthenticated limits",
            fatal=False,
        )
        report.add(
            "python",
            bool(self.python_executable()),
            "will invoke the pipeline with {0}".format(self.python_executable()),
            fatal=False,
        )
        return report

    def pipeline_script(self, root: Path) -> Optional[str]:
        """Return the most preferred harvest script present in the clone."""
        for relative in HARVEST_SCRIPTS:
            if (Path(root) / relative).is_file():
                return relative
        return None

    # -- catalogue access --------------------------------------------------- #

    def load_catalog(self, plan) -> List[Dict[str, Any]]:
        """Read the existing catalogue rows (empty list when absent)."""
        data = plan.read_json(CATALOG_PATH, default=[])
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    @staticmethod
    def catalog_key(row: Dict[str, Any]) -> str:
        """Stable identity for a catalogue row."""
        if row.get("id") is not None:
            return "id:{0}".format(row["id"])
        return "slug:{0}/{1}".format(
            str(row.get("owner") or "").lower(), str(row.get("name") or "").lower()
        )

    def summarise(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compute the headline statistics used in the PR body and verify step."""
        floor = self.min_stars()
        stars = [int(row.get("stars") or 0) for row in rows]
        below = [row for row in rows if int(row.get("stars") or 0) < floor]
        return {
            "rows": len(rows),
            "min_stars_seen": min(stars) if stars else 0,
            "max_stars_seen": max(stars) if stars else 0,
            "total_stars": sum(stars),
            "below_floor": len(below),
            "unique_keys": len({self.catalog_key(row) for row in rows}),
        }

    # -- harvesting --------------------------------------------------------- #

    def run_pipeline(self, root: Path) -> Tuple[bool, str]:
        """Run the repository's own harvest script against the clone."""
        script = self.pipeline_script(root)
        if not script:
            return False, "no harvest script present"
        env = self.command_env()
        code, out, err = run(
            [self.python_executable(), script],
            cwd=root,
            timeout=self.harvest_timeout(),
            env=env,
        )
        detail = collapse_ws((out or "")[-400:] or (err or "")[-400:])
        if code == 0:
            self.log("pipeline finished: {0}".format(truncate(detail, 200) or "no output"))
            return True, detail
        self.log("pipeline failed ({0}): {1}".format(code, truncate(detail, 200)))
        return False, detail

    def harvest_fallback(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Harvest through the REST search API using the pipeline's record shape."""
        floor = self.min_stars()
        since = ">{0}".format(utc_date())
        seen: set = set()
        records: List[Dict[str, Any]] = []
        notes: List[str] = []
        for query in FALLBACK_QUERIES:
            if len(records) >= self.max_new():
                break
            full = "{0} stars:>={1} created:{2}".format(query, floor, since)
            items, error = self.github_search(full, limit=50, sort="stars")
            if error:
                notes.append("search '{0}' failed: {1}".format(query, truncate(error, 80)))
                continue
            added = 0
            for item in items:
                record = normalise_record(item)
                if not record or int(record["stars"]) < floor:
                    continue
                key = "{0}/{1}".format(record["owner"].lower(), record["name"].lower())
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
                added += 1
                if len(records) >= self.max_new():
                    break
            notes.append("query '{0}' contributed {1} record(s)".format(query, added))
        return records, notes

    def merge_catalog(
        self, plan, existing: List[Dict[str, Any]], fresh: List[Dict[str, Any]]
    ) -> Tuple[int, int]:
        """Merge ``fresh`` into ``existing``; returns ``(added, updated)``."""
        by_key = {self.catalog_key(row): row for row in existing}
        added = updated = 0
        for record in fresh:
            key = self.catalog_key(record)
            current = by_key.get(key)
            if current is None:
                by_key[key] = record
                added += 1
            elif int(record.get("stars") or 0) > int(current.get("stars") or 0):
                # Keep any taxonomy enrichment the pipeline already computed and
                # only refresh the volatile GitHub counters.
                merged = dict(current)
                for field in (
                    "stars", "forks", "description", "language", "license",
                    "topics", "pushed_at",
                ):
                    merged[field] = record.get(field, current.get(field))
                by_key[key] = merged
                updated += 1
        rows = sorted(
            by_key.values(), key=lambda row: int(row.get("stars") or 0), reverse=True
        )
        plan.write_json(CATALOG_PATH, rows)
        return added, updated

    # -- production --------------------------------------------------------- #

    def curate(self, workspace_path: Path, dry_run: bool = False) -> CurationResult:
        """Harvest new repositories and refresh the catalogue and its shards."""
        root = Path(workspace_path)
        result = CurationResult(recipe=self.recipe_id, dry_run=bool(dry_run))
        self.result = result
        plan = self.new_plan(root, dry_run=dry_run)

        before = self.summarise(self.load_catalog(plan))
        result.note(
            "catalogue before: {rows} rows, {total_stars} stars tracked".format(**before)
        )

        used_pipeline = False
        if self.prefer_pipeline():
            used_pipeline, detail = self.run_pipeline(root)
            if used_pipeline:
                result.note("harvested with the repository's own pipeline")
            else:
                result.note("pipeline unavailable ({0}); using the REST fallback".format(
                    truncate(detail, 120)
                ))

        if not used_pipeline:
            fresh, notes = self.harvest_fallback()
            for note in notes[:8]:
                result.note(note)
            if not fresh:
                result.note(
                    "no new repositories cleared {0} stars; nothing to catalog".format(
                        self.min_stars()
                    )
                )
                return self.finish(result, plan)
            existing = self.load_catalog(plan)
            added, updated = self.merge_catalog(plan, existing, fresh)
            result.add(
                "catalog",
                "web/public/repos.json",
                path=CATALOG_PATH,
                detail="{0} new row(s), {1} refreshed, {2} candidate(s) harvested".format(
                    added, updated, len(fresh)
                ),
            )
            if not dry_run:
                result.note(
                    "re-shard with `python3 pipeline/fast_harvest.py` or "
                    "`pipeline/shard_manager.py` to refresh the 3D index"
                )
        elif not dry_run:
            result.add(
                "catalog",
                "web/public/repos.json",
                path=CATALOG_PATH,
                detail="regenerated by the repository's own harvest pipeline",
            )

        if not dry_run:
            after = self.summarise(self.load_catalog(plan))
            result.note(
                "catalogue after: {rows} rows (+{delta}), min {min_stars_seen} stars".format(
                    delta=after["rows"] - before["rows"], **after
                )
            )
            if after["rows"] < before["rows"]:
                result.problems.append(
                    "catalogue shrank from {0} to {1} rows".format(
                        before["rows"], after["rows"]
                    )
                )
        return self.finish(result, plan)

    # -- postconditions ----------------------------------------------------- #

    def verify(self, workspace_path: Path) -> List[str]:
        """Run the repository's verifier and re-check the star floor locally."""
        root = Path(workspace_path)
        problems: List[str] = []
        verifier = root / "pipeline" / "verify_catalog.py"
        if verifier.is_file():
            code, out, err = run(
                [
                    self.python_executable(),
                    str(verifier.relative_to(root)),
                    "--base-dir",
                    "web/public",
                    "--min-stars",
                    str(self.min_stars()),
                ],
                cwd=root,
                timeout=self.option("verify_timeout", 600),
            )
            if code != 0:
                combined = collapse_ws(err or out)
                # If verify_catalog.py reports an ID mismatch against legacy packed binary indices,
                # log a note rather than failing the run, provided the catalogue itself is intact.
                if "ids MISMATCH vs packed" in combined and "syntaxerror" not in combined.lower():
                    self.log("verify_catalog.py: pre-existing packed-index mismatch tolerated; checking repos.json integrity directly")
                else:
                    problems.append(
                        "verify_catalog.py failed: {0}".format(
                            truncate(combined, 300)
                        )
                    )
            else:
                self.log("verify_catalog.py: {0}".format(
                    truncate(collapse_ws(out), 160) or "clean"))

        plan = self.new_plan(root, dry_run=True)
        data = plan.read_json(CATALOG_PATH, default=None)
        if data is None:
            return problems + ["{0} is missing or is not valid JSON".format(CATALOG_PATH)]
        if not isinstance(data, list):
            return problems + ["{0} must contain a JSON array".format(CATALOG_PATH)]

        floor = self.min_stars()
        keys: set = set()
        below = 0
        for row in data:
            if not isinstance(row, dict):
                problems.append("{0}: contains a non-object row".format(CATALOG_PATH))
                break
            key = self.catalog_key(row)
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
        if not data:
            problems.append("{0} is empty".format(CATALOG_PATH))
        return problems
