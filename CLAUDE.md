# automation-portfolio — instructions for Claude

Public repo (github.com/basartuncay/automation-portfolio). Every commit is published; treat it as a portfolio.

## Layout
- `projects/<name>/` — ~30 independent projects, each with its own `pyproject.toml` + `uv.lock`, README and tests. No shared code between projects.
- Flagships (Docker Compose + `e2e.sh` + `demo/`): invoice-platform, ops-agent, sync-hub, revenue-pipeline, stock-sync, and the others with an `e2e.sh`.
- Exceptions: `filament-ops-panel` (Laravel, code in `src/`), `n8n-invoice-flow` (n8n JSON + shell test), `apify-niche-finder` (single script), `voice-reservation` (loose files).
- `reports/`, `research_notes/` are private notes and gitignored. Never move their content into tracked files.
- `tools/` — repo-level helpers.

## Commands (run inside `projects/<name>/`)
- Python tests: `uv sync --all-extras && uv run pytest -q -rs`
- End-to-end (flagships): `./e2e.sh` (needs Docker)
- Laravel panel: `cd projects/filament-ops-panel/src && php artisan test`
- Niche finder: `python3 projects/apify-niche-finder/niche_finder.py test`
- CI (`.github/workflows/tests.yml`) runs all of the above; a new Python project must be added to its `matrix.project` list.

## Rules
- Each project has its own `.env`; never commit one. Only `.env.example` with fake values.
- Sample data must be synthetic. No real customer, invoice or personal data.
- Keep a change inside one project unless asked otherwise; update that project's README when behaviour changes.
- Update the project table in the root `README.md` when adding or renaming a project.
