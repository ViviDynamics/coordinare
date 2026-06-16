"""E2E integration test for spec 063 Phase 1: manual-override service inference.

Exercises the full deterministic path without an LLM:

  1. Write a `.coordinare/score.json` describing a service.
  2. `apply_manual_override(...)` drops services.json + start/stop/health
     scripts into `<output_root>/services/`.
  3. The workspace-side helper `_start_env_cache_services` runs the start
     script — the real (fake) service binds a TCP port.
  4. A real TCP connect proves the service is live.
  5. `stop_all_env_cache_services` runs the stop script and the port is
     released.

The "service" is a tiny bash → python listener shim so the test is hermetic
and doesn't depend on redis-server flag conventions. It still exercises the
same code paths a redis manifest would: template render → idempotent start →
SIGTERM stop.
"""

from __future__ import annotations

import json
import socket
import sys
import textwrap
import time
from pathlib import Path

import pytest
from coordinare_service_inference.manual_override import (
    apply_manual_override,
)


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _port_open(port: int, timeout: float = 1.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _write_fake_service(bin_dir: Path) -> Path:
    """A bash shim that parses --port=N and exec's a long-lived TCP listener.

    Using `exec` keeps the PID stable across the bash → python handoff, so the
    PID file written by services-start.sh (capturing $! of the background bash)
    still tracks the listener and `kill <pid>` from services-stop.sh terminates
    it cleanly.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "fakesvc"
    py = sys.executable
    fake.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            PORT=""
            for arg in "$@"; do
              case "$arg" in
                --port=*) PORT="${{arg#--port=}}" ;;
              esac
            done
            exec {py} -c "
            import socket, time
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', int(${{PORT}})))
            s.listen(1)
            while True:
                time.sleep(60)
            "
            """
        )
    )
    fake.chmod(0o755)
    return fake


@pytest.mark.asyncio
async def test_manual_override_end_to_end_starts_and_stops_service(
    tmp_path: Path,
) -> None:
    # Late imports so the module-level registry state is fresh per session.
    from performer.workspace import (
        _ACTIVE_SERVICE_CACHES,
        _start_env_cache_services,
        stop_all_env_cache_services,
    )

    _ACTIVE_SERVICE_CACHES.clear()

    fake_binary = _write_fake_service(tmp_path / "bin")
    port = _free_port()

    project = tmp_path / "project"
    (project / ".coordinare").mkdir(parents=True)
    score = {
        "services": [
            {
                "name": "fakesvc",
                "binary": str(fake_binary),
                "version": "1.0",
                "data_dir": str(tmp_path / "data"),
                "port": port,
                "why_needed": "integration test for spec 063 Phase 1",
                "sources": [".coordinare/score.json"],
            }
        ],
        "cache_inputs": [".coordinare/score.json"],
    }
    (project / ".coordinare" / "score.json").write_text(json.dumps(score))

    env_cache = tmp_path / "env-cache"
    # Scope PID/socket dir to tmp_path so prior runs can't leak into this one
    # (services-start.sh uses $XDG_RUNTIME_DIR, falling back to /tmp).
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    svc_env = {"XDG_RUNTIME_DIR": str(runtime_dir)}

    # ---- Step 1+2: manual_override drops artifacts ---------------------------
    result = apply_manual_override(
        project_root=project,
        output_root=env_cache,
        run_validation=False,
    )
    assert result.applied is True, result.reason
    services_dir = env_cache / "services"
    for fname in ("services.json", "services-start.sh", "services-stop.sh", "services-health.sh"):
        assert (services_dir / fname).is_file(), f"missing artifact: {fname}"

    # ---- Step 3: workspace start helper launches the real binary ------------
    try:
        await _start_env_cache_services(str(env_cache), svc_env)

        # ---- Step 4: real TCP connect proves the service is live ------------
        assert _wait_until(
            lambda: _port_open(port), timeout=5.0
        ), f"fakesvc did not bind 127.0.0.1:{port} after services-start.sh ran"

        # Idempotency: a second start must not relaunch (PID file + kill -0 check).
        pid_file = runtime_dir / "coordinare-services" / "fakesvc.pid"
        pid_before = pid_file.read_text().strip() if pid_file.exists() else None
        await _start_env_cache_services(str(env_cache), svc_env)
        pid_after = pid_file.read_text().strip() if pid_file.exists() else None
        assert pid_before == pid_after, "second services-start.sh invocation must be a no-op"

        # ---- Step 5: stop_all releases the port -----------------------------
        stop_all_env_cache_services()
        assert _wait_until(
            lambda: not _port_open(port), timeout=5.0
        ), f"fakesvc still listening on 127.0.0.1:{port} after services-stop.sh ran"
    finally:
        # Defence in depth: never leak a python listener if assertions fail mid-run.
        stop_all_env_cache_services()


# --------------------- spec 091: stateful postgres (US3) ---------------------


def test_manual_override_postgres_declaration_renders_init_block(tmp_path: Path) -> None:
    """091/T021/T023/FR-008/SC-005: a .coordinare/score.json declaring a postgres
    service with an init block flows verbatim through apply_manual_override → render,
    producing a services-start.sh with the coordinare-owned postgres init recipe. The
    durable declaration is trusted over (here, empty) LLM inference: apply_manual_override
    short-circuits inference and forces agent_version='manual-override'.
    """
    project = tmp_path / "project"
    (project / ".coordinare").mkdir(parents=True)
    score = {
        # Operator typed a different version; manual_override must force-correct it.
        "agent_version": "operator-typed",
        "services": [
            {
                "name": "postgres",
                "binary": "postgres",
                "version": "16",
                "data_dir": "/tmp/pg-data",
                "port": 5432,
                "why_needed": "Primary application database",
                "sources": [".coordinare/score.json"],
                "kind": "postgres",
                "init": {
                    "superuser": "root",
                    "databases": ["app_dev", "app_test"],
                    "password_env_var": "POSTGRES_PASSWORD",
                },
            }
        ],
        "cache_inputs": [".coordinare/score.json"],
    }
    (project / ".coordinare" / "score.json").write_text(json.dumps(score))

    env_cache = tmp_path / "env-cache"
    result = apply_manual_override(
        project_root=project, output_root=env_cache, run_validation=False
    )

    assert result.applied is True, result.reason
    # Durable declaration trusted over inference; agent_version force-tagged.
    assert result.manifest is not None
    assert result.manifest.agent_version == "manual-override"
    # T023: kind + init carried through verbatim (not dropped by the override path).
    svc = result.manifest.services[0]
    assert svc.kind == "postgres"
    assert svc.init is not None
    assert svc.init.superuser == "root"
    assert svc.init.databases == ["app_dev", "app_test"]
    assert svc.init.password_env_var == "POSTGRES_PASSWORD"

    # The rendered start script carries the coordinare-owned postgres init recipe.
    start = (env_cache / "services" / "services-start.sh").read_text()
    assert "initdb" in start
    assert 'if [ ! -f "$_PGDATA/PG_VERSION" ]; then' in start
    assert "createdb" in start
    assert "--pwfile=<(printf '%s' \"${POSTGRES_PASSWORD}\")" in start
    # Readiness failure is surfaced as environment-attributed, not a hang (C-12).
    assert "ERROR: env: postgres did not become ready" in start
    # Teardown is the clean fast shutdown (C-13b).
    stop = (env_cache / "services" / "services-stop.sh").read_text()
    assert "pg_ctl stop -m fast -D" in stop


@pytest.mark.asyncio
async def test_postgres_start_failure_is_environment_attributed(tmp_path: Path) -> None:
    """091/T022/T024/FR-005/SC-003: a stateful service's init/start/readiness failure
    surfaces a non-zero services-start.sh exit, which the workspace start helper routes
    into the environment_error channel (spec-088 wiring) — NOT judged as code-under-test.

    Deterministic + fast: we drop a services-start.sh that emits the SAME env-attributed
    readiness-timeout surface the postgres template renders (`ERROR: env: ... exit 75`)
    rather than waiting out a real 60s probe against an absent server.
    """
    from performer.workspace import (
        _start_env_cache_services,
        consume_services_start_failure,
    )

    # Clear any residual single-shot failure from an earlier test in this process.
    consume_services_start_failure()

    services_dir = tmp_path / "env-cache" / "services"
    services_dir.mkdir(parents=True)
    start = services_dir / "services-start.sh"
    start.write_text(
        "#!/usr/bin/env bash\n"
        'echo "ERROR: env: postgres did not become ready within 60s" >&2\n'
        "exit 75\n"
    )
    start.chmod(0o755)

    await _start_env_cache_services(str(tmp_path / "env-cache"), {})

    failure = consume_services_start_failure()
    assert failure is not None, "non-zero services-start must populate the env channel"
    assert "returncode=75" in failure
    assert "did not become ready" in failure
