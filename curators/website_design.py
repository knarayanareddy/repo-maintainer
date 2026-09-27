"""Curator recipe for ``knarayanareddy/WebsitedesignandPrompts``.

The repository is a curated gallery of website templates, each shipped with
the prompt that describes how to rebuild it. The daily expansion therefore
has to add *designs*, not data rows, so this recipe:

1. inventories the existing template folders and refuses to duplicate them;
2. asks the free Gemini tier for five genuinely distinct concepts
   (distinct = different archetype, layout rhythm and palette), falling back to
   a rotating built-in catalogue when the model is unavailable;
3. renders each concept as a standalone, fully self-contained
   ``index.html`` — responsive, modern vanilla CSS/JS, fluid type scale, and
   **zero** remote image references so GitHub Pages never renders a broken
   placeholder (all "imagery" is inline SVG or a CSS gradient);
4. writes the ``ADAPTED_PROMPT.md`` that reproduces the design and a
   ``README.md`` with the design tokens and an adaptation guide;
5. indexes the new templates in the root ``README.md`` catalog inside a
   marker-delimited block, so re-running the recipe updates one table rather
   than appending duplicates forever.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import (
    CURATION_END,
    CURATION_START,
    CheckReport,
    CurationRecipe,
    CurationResult,
    FilePlan,
    collapse_ws,
    slugify,
    truncate,
    utc_date,
)

__all__ = ["WebsiteDesignRecipe", "ThemeConcept", "ARCHETYPES"]

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
#: Anything matching this inside generated HTML is a remote asset reference.
_REMOTE_ASSET_RE = re.compile(r"""(?:src|href)\s*=\s*["']https?://|url\(\s*["']?https?://""", re.I)
#: Directories in the repo root that are infrastructure, not templates.
_RESERVED_DIRS = {
    ".github", ".git", "docs", "node_modules", "scripts", "public", "staging",
    "src", "web", "pipeline", "tests", "workers", "meta", "schemas",
}



@dataclass
class ThemeConcept:
    """One website design to be generated for a single day."""

    slug: str
    name: str
    archetype: str
    tagline: str
    palette: Dict[str, str]
    fonts: Dict[str, str] = field(default_factory=dict)
    sections: List[Dict[str, str]] = field(default_factory=list)
    concept_prompt: str = ""
    tags: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """Serialise for prompts and JSON reports."""
        return {
            "slug": self.slug,
            "name": self.name,
            "archetype": self.archetype,
            "tagline": self.tagline,
            "palette": dict(self.palette),
            "fonts": dict(self.fonts),
            "sections": [dict(item) for item in self.sections],
            "concept_prompt": self.concept_prompt,
            "tags": list(self.tags),
        }


#: Deterministic concept catalogue. Used whenever the model is unavailable and
#: as the "already covered" hint when it is, so the two paths never collide.
ARCHETYPES: Tuple[Dict[str, Any], ...] = (
    {
        "slug": "brutalist-ledger",
        "name": "Brutalist Ledger",
        "archetype": "brutalist editorial",
        "tagline": "Raw concrete, hard rules, and one screaming accent colour.",
        "palette": {"bg": "#F2F0EB", "surface": "#FFFFFF", "ink": "#111111",
                    "muted": "#6B6B6B", "accent": "#FF3B00", "accent2": "#1B3CFF"},
        "fonts": {"display": "'Archivo Black', 'Arial Black', sans-serif",
                  "body": "'IBM Plex Mono', 'SFMono-Regular', monospace"},
        "tags": ["brutalism", "editorial", "high-contrast"],
    },
    {
        "slug": "soft-serve-observatory",
        "name": "Soft Serve Observatory",
        "archetype": "ambient product showcase",
        "tagline": "Slow gradients and generous whitespace for hardware nobody can touch.",
        "palette": {"bg": "#0E1116", "surface": "#161B22", "ink": "#F4F7FB",
                    "muted": "#9AA7B8", "accent": "#7CC4FF", "accent2": "#C7A6FF"},
        "fonts": {"display": "'Sora', 'Helvetica Neue', sans-serif",
                  "body": "'Inter', system-ui, sans-serif"},
        "tags": ["hardware", "ambient", "dark"],
    },
    {
        "slug": "civic-atlas",
        "name": "Civic Atlas",
        "archetype": "data map narrative",
        "tagline": "A scrolling atlas where every number is a place you can visit.",
        "palette": {"bg": "#F7F5F0", "surface": "#FFFFFF", "ink": "#15202B",
                    "muted": "#5A6B7C", "accent": "#1F6F5C", "accent2": "#D96459"},
        "fonts": {"display": "'Fraunces', Georgia, serif",
                  "body": "'Public Sans', system-ui, sans-serif"},
        "tags": ["data", "map", "narrative"],
    },
    {
        "slug": "terminal-lounge",
        "name": "Terminal Lounge",
        "archetype": "developer tool landing",
        "tagline": "A calm, keyboard-first home for a very opinionated CLI.",
        "palette": {"bg": "#0B0F0C", "surface": "#111813", "ink": "#DCEFE0",
                    "muted": "#7F9C86", "accent": "#9AE66E", "accent2": "#FFD166"},
        "fonts": {"display": "'JetBrains Mono', 'SFMono-Regular', monospace",
                  "body": "'Inter', system-ui, sans-serif"},
        "tags": ["developer-tools", "cli", "green"],
    },
    {
        "slug": "signal-and-static",
        "name": "Signal and Static",
        "archetype": "experimental audio",
        "tagline": "Waveforms as the only navigation, because words get in the way.",
        "palette": {"bg": "#120F1C", "surface": "#1C1730", "ink": "#F3EEFF",
                    "muted": "#A093C4", "accent": "#FF5C8A", "accent2": "#4CE0C8"},
        "fonts": {"display": "'Unbounded', 'Helvetica Neue', sans-serif",
                  "body": "'Space Grotesk', system-ui, sans-serif"},
        "tags": ["audio", "experimental", "motion"],
    },
    {
        "slug": "paper-crane-academy",
        "name": "Paper Crane Academy",
        "archetype": "course landing",
        "tagline": "Warm, friendly, and structured enough to actually finish a syllabus.",
        "palette": {"bg": "#FFF8ED", "surface": "#FFFFFF", "ink": "#2B2118",
                    "muted": "#7A6A58", "accent": "#E0653A", "accent2": "#3B7EA1"},
        "fonts": {"display": "'Bricolage Grotesque', Georgia, serif",
                  "body": "'Karla', system-ui, sans-serif"},
        "tags": ["education", "friendly", "warm"],
    },
    {
        "slug": "night-shift-radio",
        "name": "Night Shift Radio",
        "archetype": "schedule / broadcast",
        "tagline": "A late-night programme guide where the schedule is the hero.",
        "palette": {"bg": "#080A14", "surface": "#101426", "ink": "#EDF0FF",
                    "muted": "#8E96C4", "accent": "#F2C14E", "accent2": "#5BC0EB"},
        "fonts": {"display": "'Archivo', 'Helvetica Neue', sans-serif",
                  "body": "'Inter', system-ui, sans-serif"},
        "tags": ["broadcast", "schedule", "night"],
    },
    {
        "slug": "terraced-fields",
        "name": "Terraced Fields",
        "archetype": "sustainability report",
        "tagline": "Serious climate reporting that refuses to look like a PDF.",
        "palette": {"bg": "#F1F5EC", "surface": "#FFFFFF", "ink": "#1F2A1C",
                    "muted": "#5F6F58", "accent": "#3F7D3A", "accent2": "#C77B2B"},
        "fonts": {"display": "'Newsreader', Georgia, serif",
                  "body": "'Source Sans 3', system-ui, sans-serif"},
        "tags": ["climate", "report", "editorial"],
    },
    {
        "slug": "kinetic-type-lab",
        "name": "Kinetic Type Lab",
        "archetype": "type specimen",
        "tagline": "A specimen sheet that animates every glyph it introduces.",
        "palette": {"bg": "#171717", "surface": "#1F1F1F", "ink": "#FAFAFA",
                    "muted": "#8C8C8C", "accent": "#D7FF3E", "accent2": "#FF6B4A"},
        "fonts": {"display": "'Anton', 'Arial Black', sans-serif",
                  "body": "'Inter', system-ui, sans-serif"},
        "tags": ["typography", "motion", "specimen"],
    },
    {
        "slug": "harbour-logistics",
        "name": "Harbour Logistics",
        "archetype": "B2B operations dashboard",
        "tagline": "A supply-chain control room that a non-technical buyer can read.",
        "palette": {"bg": "#0C1A18", "surface": "#132A26", "ink": "#E8F5F1",
                    "muted": "#7FA39A", "accent": "#2DD4A7", "accent2": "#F97362"},
        "fonts": {"display": "'Manrope', 'Helvetica Neue', sans-serif",
                  "body": "'Inter', system-ui, sans-serif"},
        "tags": ["b2b", "dashboard", "operations"],
    },
    {
        "slug": "quiet-museum",
        "name": "Quiet Museum",
        "archetype": "archive / collection",
        "tagline": "An object archive where the caption does all of the storytelling.",
        "palette": {"bg": "#F4F1EA", "surface": "#FFFDF8", "ink": "#24211C",
                    "muted": "#807868", "accent": "#8A3324", "accent2": "#395B4A"},
        "fonts": {"display": "'Cormorant Garamond', Georgia, serif",
                  "body": "'Karla', system-ui, sans-serif"},
        "tags": ["archive", "culture", "serif"],
    },
    {
        "slug": "gallery-of-ordinary",
        "name": "Gallery of Ordinary",
        "archetype": "minimal photography index",
        "tagline": "A type-only index that lets the pictures argue for themselves.",
        "palette": {"bg": "#EDEAE4", "surface": "#FFFFFF", "ink": "#1A1A1A",
                    "muted": "#8A8A8A", "accent": "#B4472E", "accent2": "#3E5C76"},
        "fonts": {"display": "'Libre Caslon Display', Georgia, serif",
                  "body": "'Neue Haas Grotesk', 'Helvetica Neue', sans-serif"},
        "tags": ["photography", "index", "quiet"],
    },
)

#: Default section rhythm when neither the model nor the catalogue supplies copy.
DEFAULT_SECTION_IDS = ("opening", "argument", "details", "invitation")


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _hex(value: str, fallback: str) -> str:
    """Return ``value`` when it is a hex colour, else ``fallback``."""
    return value if _HEX_RE.match(str(value or "")) else fallback


def _tokens_css(theme: ThemeConcept) -> str:
    """Render the design-token custom properties block."""
    palette = theme.palette
    fonts = theme.fonts or {}
    return "\n".join(
        [
            "    --bg: {0};".format(_hex(palette.get("bg", ""), "#101215")),
            "    --surface: {0};".format(_hex(palette.get("surface", ""), "#181C21")),
            "    --ink: {0};".format(_hex(palette.get("ink", ""), "#F5F7FA")),
            "    --muted: {0};".format(_hex(palette.get("muted", ""), "#98A2B3")),
            "    --accent: {0};".format(_hex(palette.get("accent", ""), "#4F8CFF")),
            "    --accent-2: {0};".format(_hex(palette.get("accent2", ""), "#9AE66E")),
            "    --font-display: {0};".format(
                fonts.get("display", "'Helvetica Neue', system-ui, sans-serif")),
            "    --font-body: {0};".format(
                fonts.get("body", "system-ui, -apple-system, sans-serif")),
            "    --step--1: clamp(0.83rem, 0.8rem + 0.15vw, 0.95rem);",
            "    --step-0: clamp(1rem, 0.96rem + 0.2vw, 1.13rem);",
            "    --step-1: clamp(1.27rem, 1.16rem + 0.55vw, 1.64rem);",
            "    --step-2: clamp(1.61rem, 1.36rem + 1.25vw, 2.38rem);",
            "    --step-3: clamp(2.04rem, 1.55rem + 2.45vw, 3.45rem);",
            "    --step-4: clamp(2.59rem, 1.71rem + 4.4vw, 5.1rem);",
            "    --measure: 62ch;",
            "    --gutter: clamp(1.25rem, 4vw, 4.5rem);",
            "    --radius: 14px;",
            "    --ease: cubic-bezier(0.22, 1, 0.36, 1);",
        ]
    )


def _inline_art(theme: ThemeConcept) -> str:
    """Build an inline SVG "artwork" so no remote image can ever break.

    A data-URI SVG keeps the page self-contained: GitHub Pages serves one
    request, and there is no ``<img>`` that can resolve to a 404.
    """
    palette = theme.palette
    accent = _hex(palette.get("accent", ""), "#4F8CFF")
    accent2 = _hex(palette.get("accent2", ""), "#9AE66E")
    ink = _hex(palette.get("ink", ""), "#F5F7FA")
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1200 800' "
        "preserveAspectRatio='xMidYMid slice'>"
        "<defs>"
        "<linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0%' stop-color='{0}'/>"
        "<stop offset='100%' stop-color='{1}'/>"
        "</linearGradient>"
        "<radialGradient id='h' cx='30%' cy='25%' r='70%'>"
        "<stop offset='0%' stop-color='{2}' stop-opacity='0.85'/>"
        "<stop offset='100%' stop-color='{2}' stop-opacity='0'/>"
        "</radialGradient>"
        "</defs>"
        "<rect width='1200' height='800' fill='url(#g)'/>"
        "<rect width='1200' height='800' fill='url(#h)'/>"
        "<g fill='none' stroke='{2}' stroke-opacity='0.35' stroke-width='2'>"
        "<circle cx='360' cy='300' r='150'/>"
        "<circle cx='360' cy='300' r='220'/>"
        "<circle cx='880' cy='540' r='120'/>"
        "<path d='M120 660 C 380 520, 700 700, 1080 480'/>"
        "</g>"
        "<g fill='{2}' fill-opacity='0.9'>"
        "<circle cx='360' cy='300' r='8'/><circle cx='880' cy='540' r='6'/>"
        "</g>"
        "</svg>"
    ).format(accent, accent2, ink)
    # Compact whitespace keeps the data URI small but still valid SVG.
    return re.sub(r">\s+<", "><", svg)


def _section_html(theme: ThemeConcept, index: int, section: Dict[str, str]) -> str:
    """Render one content section of a generated theme page."""
    section_id = slugify(section.get("id") or DEFAULT_SECTION_IDS[index % len(DEFAULT_SECTION_IDS)])
    title = collapse_ws(section.get("title") or "")
    lede = collapse_ws(section.get("lede") or "")
    body = collapse_ws(section.get("body") or "")
    parts = ['    <section class="panel" id="{0}">'.format(section_id)]
    if title:
        parts.append('      <h2 class="panel__title reveal">{0}</h2>'.format(_escape(title)))
    if lede:
        parts.append('      <p class="panel__lede reveal">{0}</p>'.format(_escape(lede)))
    if body:
        parts.append('      <p class="panel__body reveal">{0}</p>'.format(_escape(body)))
    bullets = section.get("bullets") or []
    if isinstance(bullets, list) and bullets:
        parts.append('      <ul class="panel__list reveal">')
        for bullet in bullets[:6]:
            text = collapse_ws(str(bullet))
            if text:

                parts.append("        <li>{0}</li>".format(_escape(text)))
        parts.append("      </ul>")
    parts.append("    </section>")
    return "\n".join(parts)


def _escape(text: str) -> str:
    """Escape text for safe interpolation into HTML."""
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


#: are filled by :func:`render_index_html` (escaped as ``{{``/``}}``).
#: Document head: tokens, base layout, header, and hero. ``{}`` placeholders
#: are filled by :func:`render_index_html` (escaped as ``{{``/``}}``).
_TEMPLATE_HEAD = """<!doctype html>
<html lang="en" data-theme="{slug}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<meta name="description" content="{tagline}">
<title>{name} — design template</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  :root {{
{tokens}
  }}
  html {{ scroll-behavior: smooth; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: var(--font-body);
    font-size: var(--step-0);
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ width: min(100% - (var(--gutter) * 2), 1200px); margin-inline: auto; }}
  .site-header {{
    position: sticky; top: 0; z-index: 20;
    display: flex; align-items: center; justify-content: space-between;
    gap: 1rem; padding: 0.9rem var(--gutter);
    background: color-mix(in srgb, var(--bg) 82%, transparent);
    backdrop-filter: blur(14px);
    border-bottom: 1px solid color-mix(in srgb, var(--ink) 12%, transparent);
  }}
  .site-header__mark {{
    font-family: var(--font-display);
    font-size: var(--step-0); letter-spacing: 0.02em; margin: 0;
  }}
  .site-nav {{ display: flex; gap: clamp(0.75rem, 2vw, 1.75rem); flex-wrap: wrap; }}
  .site-nav a {{
    color: var(--muted); text-decoration: none; font-size: var(--step--1);
    letter-spacing: 0.08em; text-transform: uppercase;
    transition: color 220ms var(--ease);
  }}
  .site-nav a:hover, .site-nav a:focus-visible {{ color: var(--accent); }}
  .hero {{ position: relative; overflow: hidden; isolation: isolate; }}
  .hero__art {{
    position: absolute; inset: 0; z-index: -2;
    background-image: url("data:image/svg+xml,{art}");
    background-size: cover; background-position: center;
    opacity: 0.5;
  }}
  .hero::after {{
    content: ""; position: absolute; inset: 0; z-index: -1;
    background: linear-gradient(180deg,
      color-mix(in srgb, var(--bg) 20%, transparent) 0%, var(--bg) 88%);
  }}
  .hero__inner {{ padding-block: clamp(5rem, 16vh, 11rem) clamp(3rem, 9vh, 6rem); }}
  .eyebrow {{
    display: inline-flex; align-items: center; gap: 0.5rem;
    font-size: var(--step--1); letter-spacing: 0.18em; text-transform: uppercase;
    color: var(--accent); margin: 0 0 1.25rem;
  }}
  .eyebrow::before {{ content: ""; width: 2.25rem; height: 2px; background: currentColor; }}
  h1 {{
    font-family: var(--font-display);
    font-size: var(--step-4); line-height: 0.98; letter-spacing: -0.03em;
    margin: 0 0 1.5rem; max-width: 18ch;
  }}
  .hero__lede {{
    font-size: var(--step-1); color: var(--muted);
    max-width: var(--measure); margin: 0 0 2.25rem;
  }}
  .actions {{ display: flex; flex-wrap: wrap; gap: 0.85rem; }}
  .button {{
    display: inline-flex; align-items: center; gap: 0.6rem;
    padding: 0.85rem 1.5rem; border-radius: 999px;
    font-size: var(--step--1); letter-spacing: 0.06em; text-transform: uppercase;
    text-decoration: none; border: 1px solid transparent;
    transition: transform 240ms var(--ease), background-color 240ms var(--ease);
  }}
  .button--primary {{ background: var(--accent); color: var(--bg); }}
  .button--ghost {{
    background: transparent; color: var(--ink);
    border-color: color-mix(in srgb, var(--ink) 28%, transparent);
  }}
  .button:hover {{ transform: translateY(-2px); }}
"""

#: Document body: stat strip, content panels, CTA, footer, and behaviour.
_TEMPLATE_BODY = """  .stats {{
    display: grid; gap: 1px; margin-block: clamp(3rem, 8vh, 5.5rem) 0;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    background: color-mix(in srgb, var(--ink) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--ink) 10%, transparent);
    border-radius: var(--radius); overflow: hidden; }}
  .stat {{ background: var(--bg); padding: 1.5rem; }}
  .stat__value {{
    font-family: var(--font-display); font-size: var(--step-2);
    color: var(--accent-2); margin: 0 0 0.35rem;
  }}
  .stat__label {{ margin: 0; color: var(--muted); font-size: var(--step--1); }}
  .panels {{ display: grid; gap: clamp(3rem, 8vh, 6rem); padding-block: clamp(3.5rem, 10vh, 7rem); }}
  .panel__title {{
    font-family: var(--font-display); font-size: var(--step-2);
    line-height: 1.12; letter-spacing: -0.02em; margin: 0 0 0.85rem;
  }}
  .panel__lede {{
    font-size: var(--step-1); color: var(--ink);
    margin: 0 0 0.85rem; max-width: var(--measure);
  }}
  .panel__body {{ color: var(--muted); margin: 0; max-width: var(--measure); }}
  .panel__list {{ color: var(--muted); padding-left: 1.15rem; max-width: var(--measure); }}
  .panel__list li + li {{ margin-top: 0.5rem; }}
  .cta {{
    background: var(--surface);
    border: 1px solid color-mix(in srgb, var(--ink) 12%, transparent);
    border-radius: var(--radius); padding: clamp(2rem, 5vw, 3.5rem);
    margin-bottom: clamp(3rem, 8vh, 5rem);
  }}
  .site-footer {{
    border-top: 1px solid color-mix(in srgb, var(--ink) 12%, transparent);
    padding-block: 2.5rem; color: var(--muted); font-size: var(--step--1);
    display: flex; flex-wrap: wrap; gap: 1rem; justify-content: space-between;
  }}
  .reveal {{
    opacity: 0; transform: translateY(18px);
    transition: opacity 700ms var(--ease), transform 700ms var(--ease);
  }}
  .reveal.is-visible {{ opacity: 1; transform: none; }}
  .skip-link:focus {{ position: static; left: auto; display: inline-block; padding: 0.5rem; }}
  @media (prefers-reduced-motion: reduce) {{
    html {{ scroll-behavior: auto; }}
    .reveal {{ opacity: 1; transform: none; transition: none; }}
    .button:hover {{ transform: none; }}
  }}
  @media (max-width: 640px) {{
    .site-header {{ flex-direction: column; align-items: flex-start; gap: 0.5rem; }}
    .hero__inner {{ padding-block: 4rem 3rem; }}
  }}
</style>
</head>
<body>
  <a class="skip-link" href="#opening" style="position:absolute;left:-9999px">Skip to content</a>
  <header class="site-header">
    <p class="site-header__mark">{name}</p>
    <nav class="site-nav" aria-label="Sections">
{nav}    </nav>
  </header>

  <main>
    <section class="hero" id="top">
      <div class="hero__art" role="presentation"></div>
      <div class="wrap hero__inner">
        <p class="eyebrow">{archetype}</p>
        <h1>{name}</h1>
        <p class="hero__lede">{tagline}</p>
        <div class="actions">
          <a class="button button--primary" href="#opening">Explore the design</a>
          <a class="button button--ghost" href="#invitation">Adapt it</a>
        </div>
        <div class="stats">
          <div class="stat"><p class="stat__value">0</p><p class="stat__label">External assets</p></div>
          <div class="stat"><p class="stat__value">0</p><p class="stat__label">Build steps</p></div>
          <div class="stat"><p class="stat__value">100%</p><p class="stat__label">Responsive</p></div>
        </div>
      </div>
    </section>

    <div class="wrap panels">
{sections}
      <section class="cta" id="invitation">
        <h2 class="panel__title reveal">Make it yours</h2>
        <p class="panel__body reveal">Every value on this page is a CSS custom
        property in <code>:root</code>. Swap the palette, the type stack, and the
        section copy and you have a different site without touching the layout.</p>
      </section>
    </div>
  </main>

  <footer class="site-footer wrap">
    <span>Part of WebsitedesignandPrompts</span>
    <span>Generated {generated}</span>
  </footer>

<script>
  (function () {{
    "use strict";
    var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var nodes = [].slice.call(document.querySelectorAll(
      ".reveal, .panel__title, .panel__lede, .panel__body, .panel__list"));
    if (reduce || !("IntersectionObserver" in window)) {{
      nodes.forEach(function (node) {{ node.classList.add("is-visible"); }});
      return;
    }}
    var observer = new IntersectionObserver(function (entries) {{
      entries.forEach(function (entry) {{
        if (!entry.isIntersecting) {{ return; }}
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      }});
    }}, {{ rootMargin: "0px 0px -8% 0px", threshold: 0.12 }});
    nodes.forEach(function (node) {{ observer.observe(node); }});

    // Progressive enhancement: the header gains contrast once the page scrolls.
    var header = document.querySelector(".site-header");
    var onScroll = function () {{
      header.style.borderBottomColor = window.scrollY > 12
        ? "color-mix(in srgb, var(--ink) 22%, transparent)"
        : "color-mix(in srgb, var(--ink) 12%, transparent)";
    }};
    window.addEventListener("scroll", onScroll, {{ passive: true }});
    onScroll();
  }})();
</script>
</body>
</html>
"""

def _nav_markup(sections: List[Dict[str, str]]) -> str:
    """Build the header navigation anchors for a theme's sections."""
    lines = []
    for index, item in enumerate(sections):
        ident = slugify(
            item.get("id") or DEFAULT_SECTION_IDS[index % len(DEFAULT_SECTION_IDS)]
        )
        label = collapse_ws(item.get("title") or item.get("id") or "Section")
        lines.append(
            '      <a href="#{0}">{1}</a>'.format(ident, _escape(label))
        )
    return "\n".join(lines)


def render_index_html(theme: ThemeConcept) -> str:
    """Render the standalone template page for ``theme``.

    One file, no build step, no external request: inline ``<style>``, inline
    ``<script>``, a system font stack, a fluid type scale, and scroll-triggered
    reveals that respect ``prefers-reduced-motion``. This is what GitHub Pages
    can serve directly from a subfolder.
    """
    sections = [dict(item) for item in (theme.sections or [])]
    section_markup = "\n".join(
        _section_html(theme, index, item) for index, item in enumerate(sections)
    )
    fields = {
        "slug": theme.slug,
        "name": _escape(theme.name),
        "tagline": _escape(theme.tagline),
        "archetype": _escape(theme.archetype),
        "tokens": _tokens_css(theme),
        "art": _inline_art(theme),
        "nav": _nav_markup(sections),
        "sections": section_markup,
        "generated": utc_date(),
    }
    return _TEMPLATE_HEAD.format(**fields) + _TEMPLATE_BODY.format(**fields)


def _token_table(theme: ThemeConcept) -> str:
    """Render the design-token reference table used by both Markdown files."""
    rows = ["| Token | Value |", "| --- | --- |"]
    for key, value in sorted(theme.palette.items()):
        rows.append("| `--{0}` | `{1}` |".format(key, value))
    for key, value in sorted((theme.fonts or {}).items()):
        rows.append("| `--font-{0}` | `{1}` |".format(key, value))
    return "\n".join(rows)




_ADAPTED_PROMPT_TEMPLATE = """# ADAPTED_PROMPT.md — {name}

> The prompt below is the complete design brief used to build and adapt this
> template. Paste it into a coding assistant (Antigravity, Claude, ChatGPT,
> Cursor, …) to reproduce the design or retarget it at your own product.

## 1. Design intent

{intent}

- **Archetype**: {archetype}
- **Tags**: {tags}
- **Deliverable**: one self-contained `index.html` (inline CSS and JS, no build
  step, no remote assets) plus this prompt and a `README.md`.

## 2. Design tokens

{tokens}

Use these values verbatim first, then diverge once the structure feels right.
Every token maps to a CSS custom property declared in `:root`, so a rebrand is
a palette swap rather than a rewrite.

## 3. Layout and section order

{sections}

## 4. Non-negotiable requirements

1. **No remote assets.** No `http(s)` `src`/`href` and no remote `url()` in CSS.
   All artwork is inline SVG data URIs or CSS gradients, so the page never shows
   a broken image placeholder.
2. **No build step.** One `index.html`, inline `<style>` and inline `<script>`,
   no bundler, no framework, no npm install.
3. **Responsive from 320px up.** Fluid type via `clamp()`, content-first
   breakpoints, and horizontal scrolling only inside deliberate overflow
   containers.
4. **Accessible by default.** Semantic landmarks, one `h1`, labelled nav,
   visible `:focus-visible` states, and a skip link.
5. **Motion is opt-out.** Animations run through
   `@media (prefers-reduced-motion: no-preference)` and the reveal script
   degrades to fully visible content when `IntersectionObserver` is missing.
6. **Modern typography.** A system font stack by default; if a webfont is
   requested, self-host it with `font-display: swap` and provide the fallback
   stack so the first paint is never invisible.

## 5. Adaptation guide

1. Duplicate the folder and rename it to your own theme slug.
2. Edit the `:root` block: `--bg`, `--surface`, `--ink`, `--muted`, `--accent`,
   `--accent-2`, `--font-display`, `--font-body`.
3. Replace the hero copy, the section titles, and the CTA labels.
4. Point `.hero__art` at your own inline SVG (or delete the element if the
   design reads better without it).
5. Re-check contrast: `--ink` on `--bg` and `--accent` on `--bg` should both
   clear 4.5:1 for body text.
6. Re-run the checks in `README.md` before publishing.

## 6. Definition of done

- [ ] Renders correctly from `file://` with the network disabled.
- [ ] No console errors and no 404s in the network tab.
- [ ] Passes the 320px, 768px, and 1440px viewport checks.
- [ ] Keyboard-only traversal reaches every interactive element.
- [ ] `prefers-reduced-motion: reduce` removes all non-essential motion.
- [ ] The design reads as `{archetype}` at a glance — not as a generic landing page.
"""


def _nav_markup(sections: List[Dict[str, str]]) -> str:
    """Build the header navigation anchors for a theme's sections."""
    lines = []
    for index, item in enumerate(sections):
        ident = slugify(
            item.get("id") or DEFAULT_SECTION_IDS[index % len(DEFAULT_SECTION_IDS)]
        )
        label = collapse_ws(item.get("title") or item.get("id") or "Section")
        lines.append('      <a href="#{0}">{1}</a>'.format(ident, _escape(label)))
    return "\n".join(lines)


def render_index_html(theme: ThemeConcept) -> str:
    """Render the standalone template page for ``theme``.

    One file, no build step, no external request: inline ``<style>``, inline
    ``<script>``, a system font stack, a fluid type scale, and scroll-triggered
    reveals that respect ``prefers-reduced-motion``. This is what GitHub Pages
    can serve directly from a subfolder.
    """
    sections = [dict(item) for item in (theme.sections or [])]
    section_markup = "\n".join(
        _section_html(theme, index, item) for index, item in enumerate(sections)
    )
    fields = {
        "slug": theme.slug,
        "name": _escape(theme.name),
        "tagline": _escape(theme.tagline),
        "archetype": _escape(theme.archetype),
        "tokens": _tokens_css(theme),
        "art": _inline_art(theme),
        "nav": _nav_markup(sections),
        "sections": section_markup,
        "generated": utc_date(),
    }
    return _TEMPLATE_HEAD.format(**fields) + _TEMPLATE_BODY.format(**fields)


def _token_table(theme: ThemeConcept) -> str:
    """Render the design-token reference table used by both Markdown files."""
    rows = ["| Token | Value |", "| --- | --- |"]
    for key, value in sorted(theme.palette.items()):
        rows.append("| `--{0}` | `{1}` |".format(key, value))
    for key, value in sorted((theme.fonts or {}).items()):
        rows.append("| `--font-{0}` | `{1}` |".format(key, value))
    return "\n".join(rows)


def _section_map(theme: ThemeConcept) -> str:
    """Render the anchor/section/role table for a theme's README."""
    sections = theme.sections or []
    if not sections:
        return (
            "| Anchor | Section | Role |\n| --- | --- | --- |\n"
            "| `#top` | Hero | Archetype, name, one-line promise |\n"
            "| `#opening` | Opening | The tension being removed |\n"
            "| `#argument` | Argument | How the mechanism works |\n"
            "| `#details` | Details | Three supporting beats |\n"
            "| `#invitation` | Invitation | One unambiguous next step |"
        )
    rows = ["| Anchor | Section | Role |", "| --- | --- | --- |"]
    for index, item in enumerate(sections):
        ident = slugify(
            item.get("id") or DEFAULT_SECTION_IDS[index % len(DEFAULT_SECTION_IDS)]
        )
        title = collapse_ws(item.get("title") or ident.replace("-", " ").title())
        lede = truncate(item.get("lede") or item.get("body") or "", 120)
        rows.append("| `#{0}` | {1} | {2} |".format(ident, title, lede or "—"))
    return "\n".join(rows)


_ADAPTED_PROMPT_TEMPLATE = """# ADAPTED_PROMPT.md — {name}

> The prompt below is the complete design brief used to build and adapt this
> template. Paste it into a coding assistant (Antigravity, Claude, ChatGPT,
> Cursor, …) to reproduce the design or retarget it at your own product.

## 1. Design intent

{intent}

- **Archetype**: {archetype}
- **Tags**: {tags}
- **Deliverable**: one self-contained `index.html` (inline CSS and JS, no build
  step, no remote assets) plus this prompt and a `README.md`.

## 2. Design tokens

{tokens}

Use these values verbatim first, then diverge once the structure feels right.
Every token maps to a CSS custom property declared in `:root`, so a rebrand is
a palette swap rather than a rewrite.

## 3. Layout and section order

{sections}

## 4. Non-negotiable requirements

1. **No remote assets.** No `http(s)` `src`/`href` and no remote `url()` in CSS.
   All artwork is inline SVG data URIs or CSS gradients, so the page never shows
   a broken image placeholder.
2. **No build step.** One `index.html`, inline `<style>` and inline `<script>`,
   no bundler, no framework, no npm install.
3. **Responsive from 320px up.** Fluid type via `clamp()`, content-first
   breakpoints, and horizontal scrolling only inside deliberate overflow
   containers.
4. **Accessible by default.** Semantic landmarks, one `h1`, labelled nav,
   visible `:focus-visible` states, and a skip link.
5. **Motion is opt-out.** Animations are gated behind
   `prefers-reduced-motion: no-preference`, and the reveal script degrades to
   fully visible content when `IntersectionObserver` is missing.
6. **Modern typography.** A system font stack by default; if a webfont is
   requested, self-host it with `font-display: swap` and provide the fallback
   stack so the first paint is never invisible.

## 5. Adaptation guide

1. Duplicate the folder and rename it to your own theme slug.
2. Edit the `:root` block: `--bg`, `--surface`, `--ink`, `--muted`, `--accent`,
   `--accent-2`, `--font-display`, `--font-body`.
3. Replace the hero copy, the section titles, and the CTA labels.
4. Point `.hero__art` at your own inline SVG, or delete the element if the
   design reads better without it.
5. Re-check contrast: `--ink` on `--bg` and `--accent` on `--bg` should both
   clear 4.5:1 for body text.
6. Re-run the checklist in `README.md` before publishing.

## 6. Definition of done

- [ ] Renders correctly from `file://` with the network disabled.
- [ ] No console errors and no 404s in the network tab.
- [ ] Passes the 320px, 768px, and 1440px viewport checks.
- [ ] Keyboard-only traversal reaches every interactive element.
- [ ] `prefers-reduced-motion: reduce` removes all non-essential motion.
- [ ] The design reads as `{archetype}` at a glance, not as a generic landing page.
"""


def render_adapted_prompt(theme: ThemeConcept) -> str:
    """Render the reproduction/adaptation prompt for a theme."""
    sections = theme.sections or []
    if sections:
        rows = []
        for index, item in enumerate(sections):
            ident = slugify(
                item.get("id") or DEFAULT_SECTION_IDS[index % len(DEFAULT_SECTION_IDS)]
            )
            title = collapse_ws(item.get("title") or ident.replace("-", " ").title())
            lede = truncate(item.get("lede") or item.get("body") or "", 200)
            rows.append(
                "{0}. **{1}** (`#{2}`) — {3}".format(
                    index + 1,
                    title,
                    ident,
                    lede or "one focused argument of 40-70 words",
                )
            )
        section_block = "\n".join(rows)
    else:
        section_block = (
            "1. **Opening** (`#opening`) — name the tension the product removes.\n"
            "2. **Argument** (`#argument`) — show the mechanism, not the adjectives.\n"
            "3. **Details** (`#details`) — three supporting beats, one per bullet.\n"
            "4. **Invitation** (`#invitation`) — a single, unambiguous next step."
        )
    return _ADAPTED_PROMPT_TEMPLATE.format(
        name=theme.name,
        archetype=theme.archetype,
        tags=", ".join(theme.tags) or theme.archetype,
        intent=theme.concept_prompt or "{0} — {1}".format(theme.name, theme.tagline),
        tokens=_token_table(theme),
        sections=section_block,
    )


_THEME_README_TEMPLATE = """# {name}

> {tagline}

A self-contained website template: one `index.html`, zero build steps, zero
remote assets. Drop the folder on GitHub Pages and it works.

- **Archetype:** {archetype}
- **Tags:** {tags}
- **Added:** {added}
- **Files:** `index.html`, `README.md`, `ADAPTED_PROMPT.md`

## Preview locally

```bash
# no install and no server required
open index.html

# or, if you prefer a local origin
python3 -m http.server 8000
```

## Design aesthetic tokens

{tokens}

## Section map

{sections}

## What makes this design work

- **Contrast** — the ink/background pair is chosen for body-text legibility
  first; the accent is reserved for states and calls to action, so colour
  always carries meaning.
- **Type** — the display and body stacks are declared once as custom
  properties, so pairing a new typeface is a two-line change.
- **Motion** — one `IntersectionObserver` drives every reveal. There is no
  animation library, and `prefers-reduced-motion` turns the whole system off.

## Adapt it to your product

1. `cp -R . ../your-theme-slug` and rename the folder.
2. Edit the `:root` block: swap `--bg`, `--surface`, `--ink`, `--muted`,
   `--accent`, `--accent-2` for your brand.
3. Replace the `<h1>`, the hero lede, and the CTA labels.
4. Keep the structure; change the voice. The layout rhythm is what makes the
   design distinctive, and it is independent of the copy.
5. Read [`ADAPTED_PROMPT.md`](./ADAPTED_PROMPT.md) for the full design brief,
   the non-negotiable requirements, and the definition of done.

## Quality checklist

- [ ] Renders from `file://` with the network disabled
- [ ] No remote `src`/`href`/`url()` references (no broken image placeholders)
- [ ] Readable at 320px, 768px, and 1440px
- [ ] Keyboard-only traversal reaches every link
- [ ] `prefers-reduced-motion: reduce` removes the reveals
- [ ] Body-text contrast clears WCAG AA (4.5:1)
"""


def render_theme_readme(theme: ThemeConcept) -> str:
    """Render the per-template README with tokens and an adaptation guide."""
    return _THEME_README_TEMPLATE.format(
        name=theme.name,
        tagline=theme.tagline,
        archetype=theme.archetype,
        tags=", ".join(theme.tags) or theme.archetype,
        added=utc_date(),
        tokens=_token_table(theme),
        sections=_section_map(theme),
    )


def _catalog_block(themes: List[ThemeConcept], date: str) -> str:
    """Render the machine-maintained catalog table for the root README."""
    lines = [
        "### Daily curation — {0}".format(date),
        "",
        "Added automatically by `repo_maintainer.py --mode curate`.",
        "",
        "| # | Template | Folder | Description | Tech Stack | Prompts & Docs |",
        "|---|---|---|---|---|---|",
    ]
    for index, theme in enumerate(themes, start=1):
        lines.append(
            "| **{0:02d}** | **{1}** | [`{2}/`](./{2}/) | {3} | "
            "Vanilla HTML5, CSS3, ES6+ | [Prompt Spec](./{2}/ADAPTED_PROMPT.md) "
            "· [Guide](./{2}/README.md) |".format(
                index, theme.name, theme.slug, theme.tagline
            )
        )
    lines += [
        "",
        "> Each template is standalone: open `index.html` and it runs. No build",
        "> step, no npm install, and no remote image placeholders.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Recipe
# --------------------------------------------------------------------------- #


class WebsiteDesignRecipe(CurationRecipe):
    """Add five new design templates plus the prompts that rebuild them."""

    recipe_id = "web-templates"
    title = "Website design template expansion"
    summary = (
        "Research five distinct web design concepts and ship each as a standalone "
        "template folder (index.html, ADAPTED_PROMPT.md, README.md), then index "
        "them in the root README catalog."
    )

    #: Section rhythm requested from the model and used by the fallback.
    SECTION_IDS = ("opening", "argument", "details", "proof")

    # -- preflight ---------------------------------------------------------- #

    def check(self, workspace_path: Path) -> CheckReport:
        """Confirm this clone is the template gallery and can be expanded."""
        root = Path(workspace_path)
        report = CheckReport(recipe=self.recipe_id)
        report.add("clone", root.is_dir(), str(root))
        report.require(root / "README.md", "root-readme", relative_to=root)

        existing = self.existing_themes(root)
        report.add(
            "templates",
            bool(existing),
            "{0} existing template folder(s): {1}".format(
                len(existing), ", ".join(existing[:8]) or "(none)"
            ),
        )
        wanted = self.items_per_run()
        report.add(
            "catalogue",
            len(ARCHETYPES) >= wanted,
            "{0} built-in concepts available as an offline fallback".format(
                len(ARCHETYPES)
            ),
        )
        report.add(
            "llm",
            self.llm_enabled,
            "Gemini synthesis enabled"
            if self.llm_enabled
            else "offline mode: using the built-in concept catalogue",
            fatal=False,
        )
        return report

    # -- configuration ------------------------------------------------------ #

    def items_per_run(self) -> int:
        """How many concepts to ship today (default five, per the recipe)."""
        return max(1, min(12, self.option("items_per_run", 5)))

    def existing_themes(self, root: Path) -> List[str]:
        """List template folder names already present in the repository."""
        base = Path(root)
        if not base.is_dir():
            return []
        found: List[str] = []
        for entry in sorted(base.iterdir()):
            name = entry.name
            if not entry.is_dir() or name.startswith(".") or name in _RESERVED_DIRS:
                continue
            if (entry / "index.html").is_file() or (entry / "ADAPTED_PROMPT.md").is_file():
                found.append(name)
        return found

    def _existing_names(self, root: Path) -> List[str]:
        """Collect the names already in use, so new concepts cannot collide."""
        names: List[str] = []
        for slug in self.existing_themes(root):
            heading = ""
            for candidate in ("ADAPTED_PROMPT.md", "README.md"):
                heading = self._first_heading(Path(root) / slug / candidate)
                if heading:
                    break
            names.append(heading or slug)
            names.append(slug)
        return names

    @staticmethod
    def _first_heading(path: Path) -> str:
        """Read the first Markdown H1 from ``path``, if it has one.

        Generated files prefix the H1 with their own file name
        (``# ADAPTED_PROMPT.md — Kinetic Type Lab``); that prefix is stripped
        so the catalog shows the design name rather than the file name.
        """
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.startswith("# "):
                    continue
                heading = collapse_ws(line[2:])
                for prefix in ("ADAPTED_PROMPT.md — ", "ADAPTED_PROMPT.md - ", "README.md — "):
                    if heading.startswith(prefix):
                        return heading[len(prefix):].strip()
                return heading
        except (OSError, UnicodeDecodeError):
            return ""
        return ""

    def result_note(self, message: str) -> None:
        """Record a note against the in-flight result, or the log before that."""
        if self.result is not None:
            self.result.note(message)
        else:
            self.log(message)

    # -- concept selection -------------------------------------------------- #

    def _select_concepts(self, root: Path) -> Tuple[List[ThemeConcept], str]:
        """Pick the day's concepts: the model first, the catalogue as fallback."""
        wanted = self.items_per_run()
        taken = {slugify(name) for name in self._existing_names(root) if name}
        if self.llm_enabled and self.llm is not None:
            concepts = self._concepts_from_model(wanted, taken)
            if len(concepts) >= wanted:
                return concepts[:wanted], "gemini"
            self.log(
                "model yielded {0}/{1} usable concepts; falling back".format(
                    len(concepts), wanted
                )
            )
        return self._concepts_from_catalogue(taken, wanted), "catalogue"

    def _concepts_from_catalogue(self, taken: set, wanted: int) -> List[ThemeConcept]:
        """Rotate through the built-in catalogue, skipping already-used slugs."""
        offset = int(utc_date().replace("-", "") or "0") % max(1, len(ARCHETYPES))
        ordered = list(ARCHETYPES[offset:]) + list(ARCHETYPES[:offset])
        picked: List[ThemeConcept] = []
        for entry in ordered:
            if len(picked) >= wanted:
                break
            slug = str(entry["slug"])
            if slug in taken:
                continue
            taken.add(slug)
            picked.append(
                ThemeConcept(
                    slug=slug,
                    name=str(entry["name"]),
                    archetype=str(entry["archetype"]),
                    tagline=str(entry["tagline"]),
                    palette=dict(entry["palette"]),
                    fonts=dict(entry.get("fonts") or {}),
                    sections=self._fallback_sections(entry),
                    concept_prompt=str(entry["tagline"]),
                    tags=list(entry.get("tags") or []),
                )
            )
        return picked

    @staticmethod
    def _catalogue_entry(slug: str) -> Optional[Dict[str, Any]]:
        """Look up a built-in concept by slug."""
        for entry in ARCHETYPES:
            if entry["slug"] == slug:
                return entry
        return None

    def _concepts_from_model(self, wanted: int, taken: set) -> List[ThemeConcept]:
        """Ask the free Gemini tier for ``wanted`` distinct design concepts."""
        archetypes = ", ".join(str(item["archetype"]) for item in ARCHETYPES)
        prompt = (
            "Propose {0} website design concepts for a template gallery. Each "
            "concept must be visually distinct from the others in palette, layout "
            "rhythm, and typographic voice.\n\n"
            "Archetypes to draw on: {1}.\n"
            "Names already in the gallery (do NOT repeat or imitate): {2}.\n\n"
            "Return JSON only: an array of objects with exactly these keys:\n"
            "  slug            lowercase-dashed, max 28 chars\n"
            "  name            short display name\n"
            "  archetype       one of: {1}\n"
            "  tagline         one sentence, max 140 chars, stating the promise\n"
            "  palette         object with bg, surface, ink, muted, accent, "
            "accent2, all #RRGGBB\n"
            "  fonts           object with display and body (CSS font stacks)\n"
            "  tags            array of 3 lowercase tags\n"
            "  concept_prompt  2 sentences on design intent and visual system\n"
            "  sections        array of {3} objects with keys id, title, lede, "
            "body, bullets (array of strings); ids in order: {4}\n"
        ).format(
            wanted,
            archetypes,
            ", ".join(sorted(taken)) or "(none)",
            len(self.SECTION_IDS),
            ", ".join(self.SECTION_IDS),
        )
        payload = self.llm.complete_json(
            prompt,
            system=(
                "You are an art director who ships self-contained static HTML "
                "templates. Be concrete about colour and type. Never reference "
                "external images or fonts."
            ),
            max_output_tokens=8192,
            temperature=1.0,
        )
        if not isinstance(payload, list):
            return []
        return self._sanitize_concepts(payload, taken, wanted)

    def _sanitize_concepts(
        self, payload: List[Any], taken: set, wanted: int
    ) -> List[ThemeConcept]:
        """Validate model output into usable, collision-free concepts."""
        concepts: List[ThemeConcept] = []
        for item in payload:
            if not isinstance(item, dict) or len(concepts) >= wanted:
                continue
            slug = slugify(str(item.get("slug") or item.get("name") or ""), max_length=28)
            if not slug or slug in taken:
                continue
            taken.add(slug)
            fallback = self._catalogue_entry(slug) or {}
            base_palette = dict(fallback.get("palette") or {})
            base_fonts = dict(fallback.get("fonts") or {})
            palette = item.get("palette") if isinstance(item.get("palette"), dict) else {}
            fonts = item.get("fonts") if isinstance(item.get("fonts"), dict) else {}
            tags = [
                str(tag).strip().lower()
                for tag in (item.get("tags") or [])
                if str(tag).strip()
            ]
            concepts.append(
                ThemeConcept(
                    slug=slug,
                    name=collapse_ws(item.get("name")) or slug.replace("-", " ").title(),
                    archetype=collapse_ws(item.get("archetype"))
                    or str(fallback.get("archetype") or "editorial"),
                    tagline=truncate(item.get("tagline"), 140) or "A distinct new design.",
                    palette={
                        key: _hex(palette.get(key), str(base_palette.get(key, "")))
                        for key in ("bg", "surface", "ink", "muted", "accent", "accent2")
                    },
                    fonts={
                        key: collapse_ws(fonts.get(key)) or str(base_fonts.get(key, ""))
                        for key in ("display", "body")
                    },
                    sections=self._sanitize_sections(item.get("sections"), fallback),
                    concept_prompt=collapse_ws(item.get("concept_prompt")),
                    tags=tags[:4] or list(fallback.get("tags") or []),
                )
            )
        return concepts

    def _sanitize_sections(
        self, raw: Any, fallback: Dict[str, Any]
    ) -> List[Dict[str, str]]:
        """Coerce model section copy into the four-part rhythm."""
        sections: List[Dict[str, str]] = []
        if isinstance(raw, list):
            for index, item in enumerate(raw[: len(self.SECTION_IDS)]):
                if not isinstance(item, dict):
                    continue
                ident = slugify(
                    item.get("id") or self.SECTION_IDS[index % len(self.SECTION_IDS)],
                    max_length=24,
                )
                bullets = [str(b) for b in (item.get("bullets") or []) if str(b).strip()]
                sections.append(
                    {
                        "id": ident,
                        "title": truncate(item.get("title"), 70)
                        or ident.replace("-", " ").title(),
                        "lede": truncate(item.get("lede"), 240),
                        "body": truncate(item.get("body"), 420),
                        "bullets": "|".join(truncate(b, 90) for b in bullets[:4]),
                    }
                )
        return sections or self._fallback_sections(fallback)

    def _fallback_sections(self, entry: Dict[str, Any]) -> List[Dict[str, str]]:
        """Deterministic section copy for catalogue and fallback concepts."""
        name = str(entry.get("name") or "This design")
        archetype = str(entry.get("archetype") or "editorial")
        tagline = str(entry.get("tagline") or "")
        return [
            {
                "id": "opening",
                "title": "The problem, stated plainly",
                "lede": "{0} is a {1} design built around one decision: {2}".format(
                    name, archetype, tagline.lower().rstrip(".")
                ),
                "body": "Lead with the tension the reader already feels, then name "
                "the mechanism that resolves it. No adjectives the design cannot prove.",
                "bullets": "",
            },
            {
                "id": "argument",
                "title": "How the mechanism works",
                "lede": "Show the structure that earns the promise above it.",
                "body": "A reader should be able to describe how this site works after "
                "one scroll. Structure first, then ornament, then motion.",
                "bullets": "",
            },
            {
                "id": "details",
                "title": "Three supporting beats",
                "lede": "Specifics that survive a sceptical reading.",
                "body": "Each beat answers one objection: who it is for, what it costs, "
                "and what happens when it is wrong.",
                "bullets": "Who it is for|A single named audience.|"
                "What it costs|Real numbers, not ranges.|"
                "When it fails|The honest failure mode.",
            },
            {
                "id": "proof",
                "title": "Proof and next step",
                "lede": "Close with evidence, then a single unambiguous action.",
                "body": "One call to action. Everything else on the page should make "
                "taking it feel obvious.",
                "bullets": "",
            },
        ]

    # -- production --------------------------------------------------------- #

    def curate(self, workspace_path: Path, dry_run: bool = False) -> CurationResult:
        """Generate the day's templates and index them in the root README."""
        root = Path(workspace_path)
        result = CurationResult(recipe=self.recipe_id, dry_run=bool(dry_run))
        self.result = result
        plan = self.new_plan(root, dry_run=dry_run)

        concepts, source = self._select_concepts(root)
        if not concepts:
            result.problems.append(
                "no design concept available: the built-in catalogue is exhausted"
            )
            return result
        result.llm_used = source == "gemini"
        self.log("selected {0} concept(s) from {1}".format(len(concepts), source))

        shipped: List[ThemeConcept] = []
        for theme in concepts:
            if (root / theme.slug).exists():
                result.note("skipped '{0}': folder already exists".format(theme.slug))
                continue
            plan.write("{0}/index.html".format(theme.slug), render_index_html(theme))
            plan.write(
                "{0}/ADAPTED_PROMPT.md".format(theme.slug), render_adapted_prompt(theme)
            )
            plan.write("{0}/README.md".format(theme.slug), render_theme_readme(theme))
            result.add(
                "template",
                theme.name,
                path="{0}/".format(theme.slug),
                detail="{0} — {1}".format(theme.archetype, truncate(theme.tagline, 90)),
            )
            shipped.append(theme)

        if shipped:
            # The block is rebuilt from *every* theme on disk, not just today's,
            # so a second run in the same day extends the table instead of
            # silently dropping the themes an earlier run added.
            known = self.existing_themes_after(root, plan)
            catalog = self._concept_by_slug(root, known, shipped)
            changed = plan.replace_block(
                "README.md",
                CURATION_START,
                CURATION_END,
                _catalog_block(catalog, utc_date()),
            )
            if changed:
                result.add(
                    "index",
                    "root README catalog",
                    path="README.md",
                    detail="indexed {0} new template(s) in the curation block".format(
                        len(shipped)
                    ),
                )
            else:
                result.note("root README catalog was already current")
            result.note(
                "GitHub Pages serves each theme at /<slug>/ once the deploy "
                "workflow runs on this branch"
            )
        else:
            result.note("every selected concept already exists; nothing to add")

        return self.finish(result, plan)

    # -- postconditions ----------------------------------------------------- #

    def existing_themes_after(self, root: Path, plan: "FilePlan") -> List[str]:
        """List every theme folder, including those this run has queued.

        A dry-run has not written anything yet, so the plan's own writes are
        folded in; that keeps the rendered catalog identical in both modes.
        """
        slugs = list(self.existing_themes(root))
        for item in plan.planned:
            parts = Path(item.path).parts
            if len(parts) > 1 and parts[0] not in slugs:
                slugs.append(parts[0])
        return sorted(slugs)

    def _concept_by_slug(
        self, root: Path, slugs: Sequence[str], shipped: Sequence[ThemeConcept]
    ) -> List[ThemeConcept]:
        """Rebuild a concept for each known slug, from disk where possible."""
        rebuilt: List[ThemeConcept] = []
        for slug in slugs:
            theme = next((item for item in shipped if item.slug == slug), None)
            if theme is not None:
                rebuilt.append(theme)
                continue
            entry = self._catalogue_entry(slug) or {}
            heading = self._first_heading(Path(root) / slug / "ADAPTED_PROMPT.md")
            rebuilt.append(
                ThemeConcept(
                    slug=slug,
                    name=heading
                    or str(entry.get("name") or slug.replace("-", " ").title()),
                    archetype=str(entry.get("archetype") or "curated template"),
                    tagline=str(entry.get("tagline") or "A curated design template."),
                    palette=dict(entry.get("palette") or {}),
                    fonts=dict(entry.get("fonts") or {}),
                    tags=list(entry.get("tags") or []),
                )
            )
        return rebuilt

    def verify(self, workspace_path: Path) -> List[str]:
        """Validate template completeness and the no-broken-images rule."""
        root = Path(workspace_path)
        problems: List[str] = []
        themes = self.existing_themes(root)
        if not themes:
            return ["no template folders were found in the repository"]

        for slug in themes:
            folder = root / slug
            for required in ("index.html", "README.md", "ADAPTED_PROMPT.md"):
                target = folder / required
                if not target.is_file():
                    problems.append("{0}: missing {1}".format(slug, required))
                elif target.stat().st_size < 400:
                    problems.append("{0}: {1} is suspiciously small".format(slug, required))

            page = folder / "index.html"
            if not page.is_file():
                continue
            html = page.read_text(encoding="utf-8", errors="replace")
            lowered = html.lower()
            if "<!doctype html>" not in lowered:
                problems.append("{0}: index.html has no doctype".format(slug))
            if 'name="viewport"' not in lowered:
                problems.append("{0}: index.html is missing the viewport meta".format(slug))
            if "<h1" not in lowered:
                problems.append("{0}: index.html has no <h1>".format(slug))
            if _REMOTE_ASSET_RE.search(html):
                problems.append(
                    "{0}: index.html references a remote asset (broken image "
                    "placeholder risk)".format(slug)
                )
            if "prefers-reduced-motion" not in lowered:
                problems.append(
                    "{0}: index.html does not respect prefers-reduced-motion".format(slug)
                )
            if ":root" not in html or "--accent" not in html:
                problems.append("{0}: index.html exposes no design tokens".format(slug))

        readme = root / "README.md"
        if readme.is_file():
            text = readme.read_text(encoding="utf-8", errors="replace")
            for slug in themes:
                if "./{0}/".format(slug) not in text:
                    problems.append(
                        "root README.md does not link to the '{0}' template".format(slug)
                    )
        return problems
