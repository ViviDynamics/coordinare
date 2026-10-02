# Test commands

Run by `preflight` before every push. A row runs only when the branch's diff touches
its paths; rows run top to bottom, cheapest first, and stop at the first failure.
`{files}` is the changed files the row matched. Mirrors `pr-ci.yml`: Lint, Test and
Build Success gate a merge; the other lanes are advisory. The Lint row is whole-tree,
not `{files}`, because CI lints fixed paths and never covers `agent/performer/tests`
(a changed-file row there flags pre-existing debt CI would not see; issue 511). The
pytest rows install `agent/performer[dev]` first because `uv run` alone resolves only
the root project and CI installs both before pytest; each pytest row also creates the
venv itself so any row is self-contained on a clean checkout (uv run rows do this
implicitly; `uv pip install` does not).

| Area | Paths | Command |
| --- | --- | --- |
| Lint | *.py | uv run --extra dev ruff check src tests agent/performer/src |
| Ruff baseline ratchet | *.py, pyproject.toml | uv run --extra dev python scripts/ruff_baseline.py |
| Typecheck | src/*.py, pyproject.toml | uv run --extra dev mypy -p coordinare |
| Chart | deploy/helm/* | helm lint deploy/helm/coordinare && uv run pytest -q tests/unit/test_147_helm_deployment.py tests/unit/test_146_kubernetes_transport.py tests/unit/test_225_kubernetes_egress.py |
| Changed unit tests | tests/unit/*.py | uv venv 2>/dev/null; uv pip install -e ".[dev]" && uv pip install -e "agent/performer[dev]" && uv run pytest -q {files} |
| Unit and integration | src/*, tests/*, pyproject.toml, uv.lock | uv venv 2>/dev/null; uv pip install -e ".[dev]" && uv pip install -e "agent/performer[dev]" && uv run pytest -q |
| Performer | agent/performer/* | uv venv 2>/dev/null; uv pip install -e ".[dev]" && uv pip install -e "agent/performer[dev]" && uv run pytest -q agent/performer/tests/ --ignore=agent/performer/tests/integration |
