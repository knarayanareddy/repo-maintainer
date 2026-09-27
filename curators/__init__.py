"""Modular, repo-specific autonomous curation recipes.

Each module in this package owns one repository's daily expansion recipe and
implements the three-verb contract declared by :class:`curators.base.
CurationRecipe`. Recipes are looked up by the identifier referenced from
``config/repos.json`` under ``curator.recipe``:

===============  =======================================
Recipe id        Repository
===============  =======================================
``web-templates``      ``knarayanareddy/WebsitedesignandPrompts``
``ai-arsenal``         ``knarayanareddy/AI-Arsenal``
``gitscour``           ``knarayanareddy/gitscour``
``ai-daily``           ``knarayanareddy/AI-Daily``
``toolscour``          ``knarayanareddy/toolscour``
===============  =======================================

Usage::

    from curators import build_recipe
    recipe = build_recipe("ai-daily", {"items_per_run": 25})
    report = recipe.check(workspace)
    if report.ok:
        result = recipe.curate(workspace, dry_run=True)
        problems = recipe.verify(workspace)
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Type

from . import base, llm
from .base import (
    CheckReport,
    CurationError,
    CurationItem,
    CurationRecipe,
    CurationResult,
    FilePlan,
)
from .ai_arsenal import AiArsenalRecipe
from .ai_daily import AiDailyRecipe
from .gitscour import GitscourRecipe
from .toolscour import ToolscourRecipe
from .website_design import WebsiteDesignRecipe

__all__ = [
    "llm",
    "base",
    "CheckReport",
    "CurationError",
    "CurationItem",
    "CurationRecipe",
    "CurationResult",
    "FilePlan",
    "REGISTRY",
    "RECIPE_IDS",
    "AiArsenalRecipe",
    "AiDailyRecipe",
    "GitscourRecipe",
    "ToolscourRecipe",
    "WebsiteDesignRecipe",
    "build_recipe",
    "known_recipes",
    "recipe_class",
]

#: Recipe identifier -> implementing class.
REGISTRY: Dict[str, Type[CurationRecipe]] = {
    WebsiteDesignRecipe.recipe_id: WebsiteDesignRecipe,
    AiArsenalRecipe.recipe_id: AiArsenalRecipe,
    GitscourRecipe.recipe_id: GitscourRecipe,
    AiDailyRecipe.recipe_id: AiDailyRecipe,
    ToolscourRecipe.recipe_id: ToolscourRecipe,
}

#: Sorted tuple of every registered recipe identifier.
RECIPE_IDS = tuple(sorted(REGISTRY))


def recipe_class(recipe_id: str) -> Type[CurationRecipe]:
    """Resolve a recipe identifier to its class.

    Raises :class:`CurationError` for an unknown id so a typo in
    ``repos.json`` fails loudly during ``--check-only`` instead of silently
    skipping a repository.
    """
    key = str(recipe_id or "").strip()
    if key not in REGISTRY:
        raise CurationError(
            "unknown curator recipe {0!r}; known recipes: {1}".format(
                key, ", ".join(RECIPE_IDS) or "(none)"
            )
        )
    return REGISTRY[key]


def build_recipe(
    recipe_id: str,
    options: Optional[Dict[str, Any]] = None,
    *,
    log: Optional[Callable[[str], None]] = None,
    use_llm: bool = True,
    client: Optional[llm.GeminiClient] = None,
) -> CurationRecipe:
    """Instantiate a recipe, wiring in a Gemini client when one is wanted.

    ``use_llm=False`` builds the recipe with no client at all, which is how
    ``--dry-run`` and the offline unit tests keep prompts out of the network.
    """
    cls = recipe_class(recipe_id)
    gemini = client
    if gemini is None and use_llm:
        gemini = llm.GeminiClient(
            enabled=not _flag_off(options),
            log=log,
        )
        if not gemini.available:
            gemini = None
    return cls(options or {}, llm=gemini, log=log)


def _flag_off(options: Optional[Dict[str, Any]]) -> bool:
    """Honour a global ``llm: false`` switch in the curator options block."""
    value = (options or {}).get("llm", True)
    if isinstance(value, str):
        return value.strip().lower() in ("0", "false", "no", "off")
    return not bool(value)


def known_recipes() -> List[Dict[str, str]]:
    """Describe every registered recipe for diagnostics and ``--check-only``."""
    return [
        {
            "id": key,
            "title": REGISTRY[key].title,
            "summary": REGISTRY[key].summary,
        }
        for key in RECIPE_IDS
    ]
