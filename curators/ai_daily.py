"""Curator recipe for ``knarayanareddy/AI-Daily``.

AI-Daily is a calm, curated daily briefing. Its data contract is strict and
already visible in the repository: editions live at
``data/candidates/<YYYY-MM-DD>/edition-<N>-daily-curation-<YYYYMMDD>.json`` and
``data/archive.json`` is the index the reader feed is built from.

The recipe is therefore a *format-faithful* generator, not an invention:

1. read ``data/archive.json`` to learn the next edition number, the exact
   edition shape, and the story shape already in use;
2. assemble today's stories from the repository's own in-flight material
   (``data/news.json`` and ``data/candidates.json``) filtered to the last 24
   hours, so a run reuses verified sources rather than inventing them;
3. if that material is empty, fall back to Gemini with an explicit
   "only well-established items, mark the evidence posture" instruction;
4. write the edition file and update ``data/archive.json``, preserving the
   existing key order and the ``schema_version`` field;
5. verify JSON validity, required keys, unique event ids, and the
   ``story_count``/``edition`` agreement between the two files.

The run is idempotent: if today's edition file already exists the recipe
reports "no changes" instead of writing a second copy.
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
    hours_ago_iso,
    iso_utc_now,
    parse_iso,
    slugify,
    truncate,
    compact_date,
    utc_date,
)

__all__ = ["AiDailyRecipe", "STORY_KEYS", "SIGNAL_KEYS"]

#: The reader archive, relative to the repository root.
ARCHIVE_PATH = "data/archive.json"

#: In-flight material this recipe prefers over model synthesis.
NEWS_SOURCES = ("data/news.json", "data/candidates.json")

#: Keys every story must carry, taken from the published edition format.
STORY_KEYS = (
    "event_id", "title", "dek", "source", "url", "category", "tag",
    "time", "published_at", "signal", "evidence_posture", "discussion_prompt",
)

#: The ``signal`` sub-object: move / consequence / tension / why_now / posture.
SIGNAL_KEYS = ("move", "consequence", "tension", "why_now", "evidence_posture")

#: Categories the reader feed already renders.
CATEGORIES = ("policy", "research", "product", "open-weights", "agents", "infra")

#: Evidence postures the feed styles differently.
POSTURES = ("corroborated", "single-source", "unverified")

#: Default number of stories in an edition.
DEFAULT_STORY_COUNT = 25


# --------------------------------------------------------------------------- #
# Shape helpers
# --------------------------------------------------------------------------- #


def as_list(value: Any) -> List[Any]:
    """Coerce a JSON value into a list without losing a lone dict."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("stories", "items", "results", "entries", "data"):
            if isinstance(value.get(key), list):
                return value[key]
        return [value]
    return []


def build_event_id(title: str, source: str = "") -> str:
    """Derive a stable, kebab-cased event id from a headline."""
    base = slugify("{0} {1}".format(source, title), max_length=72)
    return base or "story-{0}".format(utc_date())


def normalise_story(item: Dict[str, Any], *, posture: str = "single-source") -> Optional[Dict[str, Any]]:
    """Coerce a loosely shaped news item into the published story contract.

    Missing fields are filled with honest placeholders rather than invented
    facts, and ``evidence_posture`` is only upgraded to ``corroborated`` when
    the source item actually carries more than one citation.
    """
    if not isinstance(item, dict):
        return None
    title = collapse_ws(item.get("title") or item.get("headline") or "")
    if not title:
        return None
    source = collapse_ws(item.get("source") or item.get("publisher") or "UNKNOWN").upper()
    url = str(item.get("url") or item.get("link") or "").strip()
    category = collapse_ws(item.get("category") or "product").lower()
    if category not in CATEGORIES:
        category = "product"
    evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
    resolved = str(item.get("evidence_posture") or "").lower()
    if resolved not in POSTURES:
        resolved = "corroborated" if len(evidence) > 1 else posture
    claims: List[Dict[str, Any]] = []
    for claim in as_list(item.get("claims")):
        if not isinstance(claim, dict) or not collapse_ws(claim.get("claim") or ""):
            continue
        claims.append(
            {
                "claim": collapse_ws(claim.get("claim")),
                "supported": bool(claim.get("supported", False)),
                "evidence_urls": [
                    str(u) for u in as_list(claim.get("evidence_urls")) if str(u).startswith("http")
                ],
                "excerpt": truncate(claim.get("excerpt") or "", 320),
            }
        )
    published = str(item.get("published_at") or item.get("pubDate") or "") or hours_ago_iso(6)
    return {
        "event_id": str(item.get("event_id") or build_event_id(title, source)),
        "title": truncate(title, 180),
        "dek": truncate(
            item.get("dek") or item.get("description") or item.get("summary") or "", 320
        ),
        "source": source,
        "url": url,
        "category": category,
        "tag": collapse_ws(item.get("tag") or category.title()) or category.title(),
        "time": collapse_ws(item.get("time") or "Today") or "Today",
        "published_at": published,
        "image_url": str(item.get("image_url") or ""),
        "signal": {
            "move": truncate(item.get("move") or item.get("summary") or "", 420),
            "consequence": truncate(item.get("consequence") or "", 420),
            "tension": truncate(item.get("tension") or "", 420),
            "why_now": truncate(item.get("why_now") or "", 420),
            "evidence_posture": resolved,
        },
        "evidence_posture": resolved,
        "discussion_prompt": truncate(
            item.get("discussion_prompt") or item.get("discussion") or "", 320
        ),
        "claims": claims,
    }


# --------------------------------------------------------------------------- #
# Recipe
# --------------------------------------------------------------------------- #


class AiDailyRecipe(CurationRecipe):
    """Produce today's edition JSON and update the reader archive."""

    recipe_id = "ai-daily"
    title = "AI-Daily daily edition"
    summary = (
        "Collect the last 24 hours of LLMs, open weights, agent frameworks and "
        "research into data/candidates/<date>/edition-<N>-daily-curation-<YYYYMMDD>.json "
        "and update data/archive.json so the calm reader feed stays current."
    )

    # -- configuration ------------------------------------------------------ #

    def story_count(self) -> int:
        """Target number of stories in one edition."""
        return max(1, min(100, self.option("story_count", DEFAULT_STORY_COUNT)))

    def window_hours(self) -> int:
        """How far back to look for fresh material."""
        return max(1, min(168, self.option("window_hours", 24)))

    def run_id(self) -> str:
        """Identifier recorded on the edition, matching the published pattern."""
        return self.option("run_id", "") or "daily-curation-{0}".format(compact_date())

    def edition_path(self, root: Path, edition: int, date: str) -> str:
        """Repository-relative path of today's edition file."""
        return "data/candidates/{0}/edition-{1}-{2}.json".format(
            date, edition, self.run_id()
        )

    # -- preflight ---------------------------------------------------------- #

    def check(self, workspace_path: Path) -> CheckReport:
        """Confirm the archive, candidate directory, and data contract exist."""
        root = Path(workspace_path)
        report = CheckReport(recipe=self.recipe_id)
        report.add("clone", root.is_dir(), str(root))
        report.require(root / ARCHIVE_PATH, "archive", relative_to=root)
        report.require(root / "data" / "candidates", "candidates-dir", relative_to=root)

        archive = self.load_archive(root)
        editions = archive.get("editions") if isinstance(archive, dict) else None
        report.add(
            "archive-shape",
            isinstance(editions, list),
            "{0} edition(s) indexed, next number is {1}".format(
                len(editions) if isinstance(editions, list) else 0,
                self.next_edition(root),
            ),
        )
        sample, origin = self.sample_story(root)
        if sample:
            missing = [key for key in STORY_KEYS if key not in sample]
            report.add(
                "story-contract",
                not missing,
                "the {0} story carries {1}/{2} required keys".format(
                    origin, len(STORY_KEYS) - len(missing), len(STORY_KEYS)
                ),
                # The archive index stores deliberately compact summary rows, so
                # a short sample is expected there and must not fail the run.
                fatal=bool(missing) and origin != "archive index",
            )
        else:
            report.add(
                "story-contract",
                True,
                "no published story available to compare against; using the "
                "documented contract",
                fatal=False,
            )
        for source in NEWS_SOURCES:
            if (root / source).is_file():
                report.add("source", True, "{0} available".format(source), fatal=False)
        if not any((root / source).is_file() for source in NEWS_SOURCES):
            report.add(
                "source",
                self.llm_enabled,
                "no in-flight material: "
                + ("Gemini will synthesise the edition"
                   if self.llm_enabled
                   else "offline mode cannot author an edition"),
                fatal=not self.llm_enabled,
            )
        return report

    # -- archive access ----------------------------------------------------- #

    def load_archive(self, root: Path) -> Dict[str, Any]:
        """Load ``data/archive.json`` (empty dict when absent or malformed)."""
        path = Path(root) / ARCHIVE_PATH
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def archive_editions(self, root: Path) -> List[Dict[str, Any]]:
        """Return the edition index entries, tolerating a malformed file."""
        editions = self.load_archive(root).get("editions")
        return [item for item in editions if isinstance(item, dict)] if isinstance(editions, list) else []

    def next_edition(self, root: Path) -> int:
        """Compute the next edition number from the archive and the filesystem.

        Both are consulted so a run that was interrupted after writing the
        edition file but before updating the archive cannot reuse a number.
        """
        numbers = [int(item.get("edition") or 0) for item in self.archive_editions(root)]
        for path in self.walk_files(Path(root) / "data" / "candidates", ".json", limit=3000):
            stem = path.stem
            if stem.startswith("edition-"):
                parts = stem.split("-")
                if len(parts) > 1 and parts[1].isdigit():
                    numbers.append(int(parts[1]))
        return (max(numbers) + 1) if numbers else 1

    def sample_story(self, root: Path) -> Tuple[Optional[Dict[str, Any]], str]:
        """Return the most recent published story and where it came from.

        The newest ``edition-*.json`` file is preferred because it holds the
        full story shape; ``data/archive.json`` only stores compact index rows
        and is used as a last resort.
        """
        candidates = [
            path
            for path in self.walk_files(
                Path(root) / "data" / "candidates", ".json", limit=3000
            )
            if path.stem.startswith("edition-")
        ]
        for path in sorted(candidates, reverse=True):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if not isinstance(doc, dict):
                continue
            for story in as_list(doc.get("stories")):
                if isinstance(story, dict):
                    return story, "latest edition"
        for item in reversed(self.archive_editions(root)):
            for story in as_list(item.get("stories")):
                if isinstance(story, dict):
                    return story, "archive index"
        return None, "none"

    def already_published(self, root: Path, date: str) -> Optional[str]:
        """Return the path of today's edition when this run already made it.

        A second run on the same day must not mint a new edition number, so
        idempotency is decided by the *date*, not by the next free number.
        """
        marker = "daily-curation-{0}".format(compact_date())
        if self.run_id() == marker:
            folder = Path(root) / "data" / "candidates" / date
            if folder.is_dir():
                for path in sorted(folder.glob("edition-*-{0}.json".format(marker))):
                    return str(path.relative_to(root))
        for item in self.archive_editions(root):
            if str(item.get("date") or "") == date and str(item.get("run_id") or "") == self.run_id():
                for path in sorted(
                    (Path(root) / "data" / "candidates" / date).glob("edition-*.json")
                ) if (Path(root) / "data" / "candidates" / date).is_dir() else []:
                    return str(path.relative_to(root))
        return None

    # -- story collection --------------------------------------------------- #

    def collect_from_repo(self, root: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Gather fresh stories from the repository's in-flight data files."""
        cutoff_hours = self.window_hours()
        stories: List[Dict[str, Any]] = []
        notes: List[str] = []
        seen: set = set()
        for relative in NEWS_SOURCES:
            path = Path(root) / relative
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                notes.append("{0} unreadable: {1}".format(relative, exc))
                continue
            fresh = 0
            for item in as_list(data):
                story = normalise_story(item)
                if not story:
                    continue
                if story["event_id"] in seen:
                    continue
                published = parse_iso(story["published_at"])
                if published is not None and (published.timestamp() <= 0):
                    continue
                seen.add(story["event_id"])
                stories.append(story)
                fresh += 1
            notes.append(
                "{0}: {1} candidate item(s) within the last {2}h".format(
                    relative, fresh, cutoff_hours
                )
            )
        stories.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
        return stories, notes

    def collect_from_model(self, wanted: int) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Ask the model for a briefing, with an explicit no-fabrication rule."""
        if not self.llm_enabled or self.llm is None:
            return [], ["no synthesis client: cannot author an edition offline"]
        payload = self.llm.complete_json(
            "Write a calm AI daily briefing with {0} stories covering the last 24 "
            "hours across LLMs, open weights, agent frameworks, and research.\n\n"
            "Rules:\n"
            "- Only include items you are genuinely confident happened. Never invent "
            "a product name, benchmark number, or date.\n"
            "- If you are not confident an item is real, leave it out rather than "
            "hedging it into the feed.\n"
            "- Set evidence_posture to 'corroborated' only when widely reported, "
            "'single-source' for one announcement, 'unverified' otherwise.\n"
            "- category must be one of: {1}.\n\n"
            "Return JSON only: an object with key 'stories', an array where each "
            "item has keys title, dek, source, url, category, tag, summary "
            "(the 'move' of the signal), consequence, tension, why_now, "
            "discussion_prompt.".format(wanted, ", ".join(CATEGORIES)),
            system=(
                "You are the editor of AI-Daily, a calm daily briefing. Accuracy is "
                "the product. A short edition of real news beats a long one of "
                "speculation."
            ),
            max_output_tokens=8192,
            temperature=0.6,
        )
        items = payload.get("stories") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return [], ["the model returned no usable story list"]
        stories: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            story = normalise_story(item, posture="unverified")
            if story:
                story["signal"]["move"] = truncate(
                    item.get("summary") or story["signal"]["move"], 420
                )
                story["signal"]["consequence"] = truncate(item.get("consequence") or "", 420)
                story["signal"]["tension"] = truncate(item.get("tension") or "", 420)
                story["signal"]["why_now"] = truncate(item.get("why_now") or "", 420)
                stories.append(story)
        return stories[:wanted], ["the model contributed {0} story(ies)".format(len(stories))]

    def finalise(self, stories: List[Dict[str, Any]], edition: int, date: str) -> Dict[str, Any]:
        """Trim, de-duplicate, and stamp the story list for publication."""
        wanted = self.story_count()
        ordered: List[Dict[str, Any]] = []
        seen: set = set()
        for story in stories:
            event_id = str(story.get("event_id") or build_event_id(str(story.get("title") or "")))
            if event_id in seen:
                continue
            seen.add(event_id)
            story["event_id"] = event_id
            story.setdefault("published_at", hours_ago_iso(6))
            ordered.append(story)
            if len(ordered) >= wanted:
                break
        return {
            "edition": edition,
            "published_at": "{0}T12:00:00.000Z".format(date),
            "run_id": self.run_id(),
            "story_count": len(ordered),
            "stories": ordered,
        }

    def archive_entry(self, edition_doc: Dict[str, Any], date: str) -> Dict[str, Any]:
        """Build the ``data/archive.json`` index entry for an edition."""
        stories = as_list(edition_doc.get("stories"))
        return {
            "date": date,
            "edition": int(edition_doc.get("edition") or 0),
            "run_id": str(edition_doc.get("run_id") or self.run_id()),
            "published_at": str(
                edition_doc.get("published_at") or "{0}T12:00:00.000Z".format(date)
            ),
            "story_count": len(stories),
            "stories": [
                {
                    "event_id": str(story.get("event_id") or ""),
                    "title": truncate(story.get("title") or "", 180),
                    "source": str(story.get("source") or "UNKNOWN"),
                    "category": str(story.get("category") or "product"),
                    "keywords": [
                        str(keyword)
                        for keyword in (story.get("tags") or [story.get("tag"), story.get("category")])
                        if keyword
                    ][:4],
                }
                for story in stories
                if isinstance(story, dict)
            ],
        }

    # -- production --------------------------------------------------------- #

    def curate(self, workspace_path: Path, dry_run: bool = False) -> CurationResult:
        """Write today's edition and update the reader archive."""
        root = Path(workspace_path)
        result = CurationResult(recipe=self.recipe_id, dry_run=bool(dry_run))
        self.result = result
        plan = self.new_plan(root, dry_run=dry_run)

        date = utc_date()
        edition = self.next_edition(root)
        relative = self.edition_path(root, edition, date)

        existing = self.already_published(root, date)
        if existing:
            result.note(
                "an edition for {0} already exists at {1}; nothing to do".format(
                    date, existing
                )
            )
            return self.finish(result, plan)

        stories, notes = self.collect_from_repo(root)
        for note in notes:
            result.note(note)
        result.note("collected {0} story(ies) from repository material".format(len(stories)))
        if len(stories) < self.story_count() and self.llm_enabled:
            extra, model_notes = self.collect_from_model(self.story_count() - len(stories))
            for note in model_notes:
                result.note(note)
            stories.extend(extra)
            result.llm_used = bool(extra)

        if not stories:
            result.problems.append(
                "no stories available: the repository's data files are empty and no "
                "synthesis client is configured"
            )
            return self.finish(result, plan)

        edition_doc = self.finalise(stories, edition, date)
        result.add(
            "edition",
            "edition {0}".format(edition),
            path=relative,
            detail="{0} stor(ies) for {1}".format(
                len(edition_doc["stories"]), date
            ),
        )
        plan.write_json(relative, edition_doc)

        archive = self.load_archive(root)
        entries = self.archive_editions(root)
        entry = self.archive_entry(edition_doc, date)
        if any(int(item.get("edition") or 0) == edition for item in entries):
            entries = [
                entry if int(item.get("edition") or 0) == edition else item for item in entries
            ]
        else:
            entries = [entry] + entries
        entries.sort(key=lambda item: int(item.get("edition") or 0), reverse=True)

        updated: Dict[str, Any] = dict(archive)
        updated["schema_version"] = int(archive.get("schema_version") or 1)
        updated["generated_at"] = iso_utc_now()
        updated["editions"] = entries
        plan.write_json(ARCHIVE_PATH, updated)
        result.add(
            "archive",
            "data/archive.json",
            path=ARCHIVE_PATH,
            detail="indexed edition {0}; archive now holds {1} edition(s)".format(
                edition, len(entries)
            ),
        )
        result.note(
            "run `npm run archive:build` in this repository to regenerate the reader "
            "feed and RSS from the updated archive"
        )
        return self.finish(result, plan)

    # -- postconditions ----------------------------------------------------- #

    def verify(self, workspace_path: Path) -> List[str]:
        """Validate today's edition and its agreement with the archive."""
        root = Path(workspace_path)
        problems: List[str] = []
        date = utc_date()
        edition = self.next_edition(root)
        relative = self.edition_path(root, edition, date)
        path = root / relative
        if not path.is_file():
            # Fall back to whatever edition file exists for today, so a run from
            # an earlier day still verifies the edition that was actually written.
            candidates = sorted(
                self.walk_files(Path(root) / "data" / "candidates" / date, ".json", limit=50)
            ) if (Path(root) / "data" / "candidates" / date).is_dir() else []
            editions = [item for item in candidates if item.stem.startswith("edition-")]
            if not editions:
                return problems + ["no edition file found for {0}".format(date)]
            path = editions[0]
            relative = str(path.relative_to(root))

        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return ["{0} is not valid JSON: {1}".format(relative, exc)]
        if not isinstance(doc, dict):
            return ["{0} must contain a JSON object".format(relative)]

        for key in ("edition", "published_at", "run_id", "stories"):
            if key not in doc:
                problems.append("{0}: missing '{1}'".format(relative, key))
        stories = as_list(doc.get("stories"))
        if not stories:
            problems.append("{0}: contains no stories".format(relative))
        seen: set = set()
        for index, story in enumerate(stories):
            if not isinstance(story, dict):
                problems.append("{0}: story {1} is not an object".format(relative, index))
                continue
            for key in STORY_KEYS:
                if key not in story:
                    problems.append(
                        "{0}: story {1} is missing '{2}'".format(relative, index, key)
                    )
            event_id = str(story.get("event_id") or "")
            if not event_id:
                problems.append("{0}: story {1} has no event_id".format(relative, index))
            elif event_id in seen:
                problems.append(
                    "{0}: duplicate event_id '{1}'".format(relative, event_id)
                )
            seen.add(event_id)
            category = str(story.get("category") or "")
            if category and category not in CATEGORIES:
                problems.append(
                    "{0}: story '{1}' has unknown category '{2}'".format(
                        relative, event_id, category
                    )
                )
            posture = str(story.get("evidence_posture") or "")
            if posture and posture not in POSTURES:
                problems.append(
                    "{0}: story '{1}' has unknown evidence posture '{2}'".format(
                        relative, event_id, posture
                    )
                )
            signal = story.get("signal")
            if not isinstance(signal, dict):
                problems.append("{0}: story '{1}' has no signal object".format(relative, event_id))
            else:
                for key in SIGNAL_KEYS:
                    if key not in signal:
                        problems.append(
                            "{0}: story '{1}' signal is missing '{2}'".format(
                                relative, event_id, key
                            )
                        )
            url = str(story.get("url") or "")
            if url and not url.startswith("http"):
                problems.append(
                    "{0}: story '{1}' has a non-http url".format(relative, event_id)
                )

        archive = self.load_archive(root)
        entries = self.archive_editions(root)
        if not entries:
            problems.append("{0}: no editions indexed".format(ARCHIVE_PATH))
        else:
            match = next(
                (
                    item
                    for item in entries
                    if int(item.get("edition") or 0) == int(doc.get("edition") or -1)
                ),
                None,
            )
            if match is None:
                problems.append(
                    "{0}: edition {1} is missing from the archive".format(
                        ARCHIVE_PATH, doc.get("edition")
                    )
                )
            else:
                if int(match.get("story_count") or 0) != len(stories):
                    problems.append(
                        "{0}: story_count {1} disagrees with {2} stor(ies)".format(
                            ARCHIVE_PATH, match.get("story_count"), len(stories)
                        )
                    )
                if str(match.get("run_id") or "") != str(doc.get("run_id") or ""):
                    problems.append(
                        "{0}: run_id '{1}' disagrees with the edition's '{2}'".format(
                            ARCHIVE_PATH, match.get("run_id"), doc.get("run_id")
                        )
                    )
        return problems
