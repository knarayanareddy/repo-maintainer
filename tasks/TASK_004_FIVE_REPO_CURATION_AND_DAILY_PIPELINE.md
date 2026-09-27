# TASK-004: Five-Repository Autonomous Curation & Daily Expansion Pipeline

## Objective
Extend `/Users/macbookpro/.gemini/antigravity-ide/scratch/repo-maintainer/` to support repo-specific autonomous curation recipes for knarayanareddy's 5 core repositories, configure `config/repos.json`, implement modular curation handlers in `curators/`, integrate with `repo_maintainer.py`, and provide an automated daily runner script with macOS `launchd` support.

---

## Target Repositories & Specific Daily Recipes

### 1. `knarayanareddy/WebsitedesignandPrompts`
- **URL**: `https://github.com/knarayanareddy/WebsitedesignandPrompts.git`
- **Description**: Curated collection of website templates with detailed build prompts for GitHub Pages.
- **Daily Objective**:
  - Research / identify 5 distinct, aesthetically rich web design concepts slightly different from existing templates in the repo.
  - For each concept, generate a standalone subfolder (e.g. `<theme-slug>/`) containing:
    1. `index.html` (fully styled, responsive, modern Vanilla CSS/JS, modern typography, zero placeholder broken images).
    2. `ADAPTED_PROMPT.md` (the comprehensive prompt used to design and adapt the site).
    3. `README.md` (overview, design aesthetic tokens, adaptation guide).
  - Update the root `index.html` or `README.md` showcases list to index the 5 new templates so GitHub Pages serves them.

### 2. `knarayanareddy/AI-Arsenal`
- **URL**: `https://github.com/knarayanareddy/AI-Arsenal.git`
- **Description**: Machine-readable encyclopedia of AI engineering (1,600+ entries) governed by `AGENT.md` & `CONTEXT.md`.
- **Daily Objective**:
  - Parse `AGENT.md`, `CONTEXT.md`, and taxonomy schemas to understand quality gating criteria.
  - Discover newly trending or newly released AI tools, frameworks, and academic breakthroughs.
  - Generate structured markdown knowledge entries matching the existing schema and taxonomy without altering unrelated files.
  - Run linting / validation to ensure strict compliance with repo guidelines.

### 3. `knarayanareddy/gitscour`
- **URL**: `https://github.com/knarayanareddy/gitscour.git`
- **Description**: Searchable catalog of 50,000+ open-source repos (500+ stars) with 3D topology explorer.
- **Daily Objective**:
  - Leverage the existing scripts in the repo (`pipeline/fast_harvest.py` / `pipeline/harvest_scale.py`).
  - Harvest recently trending GitHub repos reaching 500+ stars using GitHub API or public archives.
  - Normalize, categorize by subsystem/domain, and update data shards.
  - Verify data integrity before staging changes.

### 4. `knarayanareddy/AI-Daily`
- **URL**: `https://github.com/knarayanareddy/AI-Daily.git`
- **Description**: A calm, curated daily briefing for the AI ecosystem.
- **Daily Objective**:
  - Collect top breakthroughs across LLMs, open-source weights, agent frameworks, and research from the past 24 hours.
  - Generate today's edition JSON in `data/candidates/YYYY-MM-DD/edition-<NUM>-daily-curation-YYYYMMDD.json` adhering to existing format.
  - Update `data/archive.json` and ensure the calm daily reader feed renders smoothly.

### 5. `knarayanareddy/toolscour`
- **URL**: `https://github.com/knarayanareddy/toolscour.git`
- **Description**: 3D spatial explorer & architecture intelligence for 11,000+ open-source AI tools and skill packs.
- **Daily Objective**:
  - Run the repo's existing `pipeline/harvest_ai_tools.py` and `pipeline/shard_builder.py`.
  - Ingest newly discovered tools, models, runtimes, and agents.
  - Re-shard the dataset to keep the 3D spatial index fresh and performant.

---

## Architectural Requirements

1. **Modular Curators Directory (`curators/`)**:
   - `curators/base.py`: Abstract Base Class defining `check()`, `curate(workspace_path, dry_run=False)`, and `verify()`.
   - `curators/website_design.py`: Recipe for `WebsitedesignandPrompts`.
   - `curators/ai_arsenal.py`: Recipe for `AI-Arsenal`.
   - `curators/gitscour.py`: Recipe for `gitscour`.
   - `curators/ai_daily.py`: Recipe for `AI-Daily`.
   - `curators/toolscour.py`: Recipe for `toolscour`.

2. **Configuration Updates (`config/repos.json`)**:
   - Add all 5 repositories to `repositories` array with proper metadata, clone depth, and enabled modes.
   - Attach the corresponding curator recipe identifier to each repo entry.
   - Validate against `config/repos.schema.json` (update schema if necessary to support `curator` field).

3. **CLI Integration**:
   - Enhance `repo_maintainer.py` to support `--curate` or a new mode `--mode curate` alongside `--mode observer` and `--mode pr`.
   - In PR mode, daily curation changes must be committed to branch `chore/daily-curation-YYYY-MM-DD` and opened via `gh pr create` with a detailed changelog.

4. **Zero-Cost LLM Synthesis**:
   - For creative text/prompt/brief synthesis (`AI-Daily`, `WebsitedesignandPrompts`, `AI-Arsenal`), connect directly to Google's free Gemini API via the key in `~/.hermes/idea-dump/keys.env` using `gemini-flash-latest` (Gemini 3.8 Flash, 15 RPM / 1M TPM free tier).

5. **Daily Scheduler (`daily_runner.py` & `com.antigravity.repo-maintainer.plist`)**:
   - Provide `daily_runner.py` that loops over enabled repos in sequence, applies rate-limits, and logs run summaries.
   - Provide macOS `launchd` plist template that triggers daily when the user's MacBook is active/awake.

---

## Verification & Acceptance Criteria
- [ ] `python3 repo_maintainer.py --check-only` passes all diagnostic checks.
- [ ] `config/repos.json` validates cleanly against `config/repos.schema.json`.
- [ ] Run `python3 repo_maintainer.py --mode observer` across the 5 repos to generate initial baseline health audits.
- [ ] Run `--dry-run` curation on at least 1 repository to demonstrate end-to-end recipe execution without unintended mutations.
