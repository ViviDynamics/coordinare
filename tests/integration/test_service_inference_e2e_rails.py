"""End-to-end smoke test (spec 063 T029): inline Rails skeleton + real Postgres.

Drives the manual-override path with a hand-authored ``.coordinare/score.json``,
runs the generated ``services-start.sh`` against a real ``postgres`` binary on
a free port, asserts ``psql -c 'SELECT 1'`` returns a row, and verifies the
generated ``services-health.sh`` exits 0 against the live cluster.

Marked ``@pytest.mark.e2e`` (excluded by default via ``addopts`` in
``pyproject.toml``). Skipped unless ``postgres``/``initdb``/``psql`` are on
``PATH`` AND ``PYTEST_E2E=1`` — protects developer machines and PR CI from a
slow, environment-dependent test.

Run locally:

    PYTEST_E2E=1 .venv/bin/pytest \\
        tests/integration/test_service_inference_e2e_rails.py -m e2e -v
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path
from textwrap import dedent

import pytest
from coordinare_service_inference.manual_override import apply_manual_override

_REQUIRED_BINARIES = ("postgres", "initdb", "psql")


def _missing_binaries() -> list[str]:
    return [b for b in _REQUIRED_BINARIES if shutil.which(b) is None]


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("PYTEST_E2E") != "1",
        reason="set PYTEST_E2E=1 to run the real-Postgres smoke test",
    ),
    pytest.mark.skipif(
        bool(_missing_binaries()),
        reason=f"missing required binaries on PATH: {_missing_binaries()}",
    ),
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_rails_skeleton(project: Path) -> None:
    """Minimal Rails-shaped repo: Gemfile + config/database.yml.

    Inline (per T029 decision) — no upstream git clone, no flake risk from a
    third-party repo being deleted or rewritten.
    """
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "Gemfile").write_text(
        dedent(
            """\
            source 'https://rubygems.org'
            gem 'rails', '~> 7.1'
            gem 'pg', '~> 1.5'
            """
        )
    )
    (project / "config" / "database.yml").write_text(
        dedent(
            """\
            default: &default
              adapter: postgresql
              encoding: unicode
              pool: 5

            development:
              <<: *default
              database: app_development
            """
        )
    )


def _score_json_for_real_postgres(
    *, data_dir: Path, port: int
) -> dict[str, object]:
    """Hand-authored manifest that boots real Postgres on the given port.

    The coordinare start.sh template redirects the service's stdout to
    ``<data_dir>/postgres.log`` *before* invoking ``start_args``, so initdb
    must target a subdirectory of ``data_dir`` (otherwise the log file makes
    the target non-empty and initdb refuses). We use ``<data_dir>/cluster``.

    Unix sockets are disabled (``-c unix_socket_directories=''``) because
    macOS tmpdirs blow past Postgres's 103-byte sun_path limit; TCP on
    127.0.0.1 is all the test needs.
    """
    cluster_dir = data_dir / "cluster"
    init_then_run = dedent(
        f"""\
        if [ ! -f {shlex.quote(str(cluster_dir))}/PG_VERSION ]; then
          initdb -D {shlex.quote(str(cluster_dir))} \\
            --auth=trust --username=postgres -A trust >/dev/null
        fi
        exec postgres \\
          -D {shlex.quote(str(cluster_dir))} \\
          -p {port} \\
          -c unix_socket_directories='' \\
          -h 127.0.0.1
        """
    ).strip()
    return {
        "services": [
            {
                "name": "postgres",
                "binary": "postgres",
                "version": "15",
                "data_dir": str(data_dir),
                "port": port,
                "why_needed": "Rails ActiveRecord adapter (postgresql in database.yml)",
                "sources": ["Gemfile", "config/database.yml"],
                "external_required": False,
                "required_env_vars": [],
                "start_args": ["bash", "-c", init_then_run],
            }
        ],
        "cache_inputs": ["Gemfile", "config/database.yml", ".coordinare/score.json"],
        "agent_version": "manual-override",
    }


def _wait_for_port(port: int, *, timeout_s: float = 15.0) -> None:
    """Block until 127.0.0.1:<port> accepts a TCP connection or timeout."""
    deadline = time.monotonic() + timeout_s
    last_err: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError as exc:
            last_err = exc
            time.sleep(0.25)
    raise TimeoutError(f"postgres did not bind 127.0.0.1:{port} within {timeout_s}s: {last_err}")


def _run_script(script: Path, *, timeout_s: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def test_rails_skeleton_real_postgres_end_to_end(tmp_path: Path) -> None:
    """Full loop: manual override → generated start.sh → live psql → health.sh → stop.sh.

    Verifies that coordinare's templater + manual-override path produce shell
    scripts which actually orchestrate a real Postgres cluster end-to-end.
    """
    project = tmp_path / "rails-app"
    _write_rails_skeleton(project)

    data_dir = tmp_path / "pgdata"
    pg_port = _free_port()

    (project / ".coordinare").mkdir()
    (project / ".coordinare" / "score.json").write_text(
        json.dumps(
            _score_json_for_real_postgres(data_dir=data_dir, port=pg_port),
            indent=2,
        )
    )

    env_cache = tmp_path / "env-cache"
    result = apply_manual_override(project, env_cache, run_validation=False)

    assert result.applied, f"manual override should apply, got: {result.reason}"
    assert result.scripts_dir is not None
    scripts_dir = result.scripts_dir

    # Verify the manifest was tagged correctly and the rendered scripts exist.
    services_json = json.loads((scripts_dir / "services.json").read_text())
    assert services_json["agent_version"] == "manual-override"
    assert [s["name"] for s in services_json["services"]] == ["postgres"]
    start_sh = scripts_dir / "services-start.sh"
    health_sh = scripts_dir / "services-health.sh"
    stop_sh = scripts_dir / "services-stop.sh"
    assert start_sh.stat().st_mode & 0o111
    assert health_sh.stat().st_mode & 0o111
    assert stop_sh.stat().st_mode & 0o111

    try:
        # Boot real postgres via the generated script.
        start = _run_script(start_sh, timeout_s=20.0)
        assert start.returncode == 0, (
            f"services-start.sh failed rc={start.returncode}\n"
            f"stdout:\n{start.stdout}\nstderr:\n{start.stderr}"
        )

        # postgres is a child process backgrounded by start.sh; wait for it to
        # bind before probing.
        _wait_for_port(pg_port, timeout_s=20.0)

        # Real DB round-trip. Trust auth + default 'postgres' database.
        psql = subprocess.run(
            [
                "psql",
                "-h", "127.0.0.1",
                "-p", str(pg_port),
                "-U", "postgres",
                "-d", "postgres",
                "-tA",
                "-c", "SELECT 1",
            ],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        assert psql.returncode == 0, (
            f"psql SELECT 1 failed rc={psql.returncode}\n"
            f"stdout:\n{psql.stdout}\nstderr:\n{psql.stderr}"
        )
        assert psql.stdout.strip() == "1", f"unexpected psql output: {psql.stdout!r}"

        # Generated health script must agree the cluster is up.
        health = _run_script(health_sh, timeout_s=10.0)
        assert health.returncode == 0, (
            f"services-health.sh failed rc={health.returncode}\n"
            f"stdout:\n{health.stdout}\nstderr:\n{health.stderr}"
        )
        assert f"listening on {pg_port}" in health.stdout

    finally:
        # Tear down even on assertion failure so we don't leak a postgres
        # process or a port binding into subsequent tests.
        if stop_sh.exists():
            stop = _run_script(stop_sh, timeout_s=15.0)
            # Best-effort: a stop failure here shouldn't mask a real assertion,
            # but surface it in CI output.
            if stop.returncode != 0:
                print(
                    f"WARNING: services-stop.sh rc={stop.returncode}\n"
                    f"stdout:\n{stop.stdout}\nstderr:\n{stop.stderr}"
                )
