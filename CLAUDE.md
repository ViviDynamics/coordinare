See AGENTS.md for all development guidelines.

## Active Technologies
- Python 3.11 (coordinare) + the existing performer image (Node + gh CLI already baked in) + `httpx` (already a transitive dep via the GitHub service), `pydantic` v2 for the rollup schema, structlog for observability (064-closer-pr-checks-gate)
- No new persistent state. Timeout anchor derives from the PR's HEAD-commit push timestamp (always re-derived from GitHub on each evaluation per FR-009). (064-closer-pr-checks-gate)

## Recent Changes
- 064-closer-pr-checks-gate: Added Python 3.11 (coordinare) + the existing performer image (Node + gh CLI already baked in) + `httpx` (already a transitive dep via the GitHub service), `pydantic` v2 for the rollup schema, structlog for observability
