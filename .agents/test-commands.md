# Test Commands for coordinare

Table of areas to local test commands, mirroring the PR CI workflow.

| Area | Command |
| --- | --- |
| Lint (Ruff) | `uv run --extra dev ruff check src tests agent/performer/src` |
| Typecheck (mypy) | `uv run --extra dev mypy -p coordinare` |
| Unit/Integration tests | `uv run pytest` |
| Chart validation | `helm lint deploy/helm/coordinare` |
| Helm rendering tests | `uv run pytest tests/unit/test_147_helm_deployment.py tests/unit/test_146_kubernetes_transport.py tests/unit/test_225_kubernetes_egress.py -v` |
| Daemon image verification | `uv run pytest tests/unit/test_194_daemon_docker_plumbing.py -v` |
| Performer tests | `uv run pytest agent/performer/` |
