# Test commands

Run by `preflight` before every push. A row runs only when the branch's diff touches
its paths; rows run top to bottom, cheapest first, and stop at the first failure.
`{files}` is the changed files the row matched. Mirrors `pr-ci.yml`: Lint, Test and
Build Success gate a merge; the other lanes are advisory.

| Area | Paths | Command |
| --- | --- | --- |
| Lint, changed files | *.py | uv run --extra dev ruff check {files} |
| Ruff baseline ratchet | *.py, pyproject.toml | uv run --extra dev python scripts/ruff_baseline.py |
| Typecheck | src/*.py, pyproject.toml | uv run --extra dev mypy -p coordinare |
| Chart | deploy/helm/* | helm lint deploy/helm/coordinare && uv run pytest -q tests/unit/test_147_helm_deployment.py tests/unit/test_146_kubernetes_transport.py tests/unit/test_225_kubernetes_egress.py |
| Changed unit tests | tests/unit/*.py | uv run pytest -q {files} |
| Unit and integration | src/*, tests/*, pyproject.toml, uv.lock | uv run pytest -q |
| Performer | agent/performer/* | uv run pytest -q agent/performer/tests/ --ignore=agent/performer/tests/integration |
