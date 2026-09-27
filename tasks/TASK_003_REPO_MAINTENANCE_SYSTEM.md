# TASK-003: Autonomous Multi-Phase GitHub Repository Maintainer

## Objective
Build a production-grade autonomous maintenance system in `/Users/macbookpro/.gemini/antigravity-ide/scratch/repo-maintainer/` that continuously reviews, maintains, and evolves specified GitHub repositories across three operational phases.

---

## Architecture & Phased Capabilities

### Phase 1: Observer Mode (Health & Staleness Audit)
- Reads target repositories configured in `config/repos.json`.
- Clones / fetches latest repository trees into an isolated workspace directory (`workspace/`).
- Runs automated audit checks:
  1. **Commit Staleness:** Days since last commit; stale open PRs/branches.
  2. **Documentation Health:** README presence, broken markdown links, missing badges, missing setup guides.
  3. **Dependency Freshness:** Identifies package managers (`package.json`, `pyproject.toml`, `requirements.txt`) and checks for outdated dependencies.
  4. **Test & CI Coverage:** Verifies existence of GitHub Actions workflows, test directories, and test suites.
- Generates a structured Markdown report: `reports/health_report_<repo>_YYYY-MM-DD.md`.
- **Zero code changes** in Phase 1 (Observer mode).

### Phase 2: Automated PR Maintenance
- Operates under strict **Branch & Pull Request Guardrails** (NEVER directly pushes to `main`).
- Creates branch: `chore/daily-maintenance-YYYY-MM-DD`.
- Performs non-destructive automated upkeep:
  - Formats code and cleans lint errors.
  - Updates README metadata, badges, and documentation links.
  - Fixes deprecation warnings or bumps safe patch versions.
- Validates changes locally by running test suites if present.
- Opens a GitHub Pull Request using `gh pr create` with an executive summary table and changelog.

### Phase 3: Autonomous Feature & Dynamic Skill Expansion
- **Dynamic Skill Discovery:**
  - Implements a skill search module leveraging Open Skills CLI (`npx skills search <term>`).
  - Supports installing discovered skills into a sandboxed directory (`~/.cline/skills/staged/`).
- **Autonomous Skill Generation:**
  - When encountering an unfamiliar tool or domain without an existing skill, formulates a new `SKILL.md` (with valid YAML frontmatter, name, description, commands).
  - Validates skill syntax before activation.
- **Feature Proposing:**
  - Proposes complementary features or architecture improvements on dedicated feature branches (`feature/<slug>`).

---

## File Structure & Deliverables
```
repo-maintainer/
├── config/
│   └── repos.json          # Target repo list with per-repo options
├── reports/                # Health & staleness audit markdown reports
├── repo_maintainer.py      # Core CLI driver implementing all 3 phases
├── skill_manager.py        # Dynamic skill discovery & sandboxed generation helper
└── README.md               # User guide & operations manual
```

### CLI Interface Requirements
- `--mode [observer|pr|feature]`: Select execution phase (default: `observer`).
- `--repo REPO_NAME`: Run on a specific repository (or all configured repos).
- `--check-only`: Diagnostic check on `gh` auth, network, and `repos.json`.
- `--dry-run`: Preview audit and proposed changes without opening PRs or writing files.
