"""Dry-run validator: exercises start → health → stop on rendered scripts.

Runs inside the bootstrap performer's container (OQ-2 resolved in plan.md). Each
phase is a subprocess invocation; we capture stdout/stderr and return a
structured result. The retry loop in `service_inference.__init__` consumes the
failure context to refine the next LLM attempt.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from coordinare.services.service_inference.templater import RenderedScripts

ValidationPhase = Literal["start", "health", "stop"]

# Health probe may need a moment after start for services to bind their ports.
# Kept short — services that take longer to boot need their own readiness logic
# inside the start script, not validator-side polling, so retries stay deterministic.
_HEALTH_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class ValidationResult:
    phase: ValidationPhase | None
    stdout: str
    stderr: str
    ok: bool
    returncode: int = 0

    @property
    def summary(self) -> str:
        if self.ok:
            return "validation passed: start → health → stop all returned 0"
        return f"validation failed in phase={self.phase} rc={self.returncode}\nstderr:\n{self.stderr}"


def validate(
    scripts: RenderedScripts,
    *,
    working_dir: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 60.0,
    start_timeout_seconds: float | None = None,
    health_timeout_seconds: float | None = None,
    stop_timeout_seconds: float | None = None,
    health_delay_seconds: float = _HEALTH_DELAY_SECONDS,
) -> ValidationResult:
    """Write the three scripts to a temp dir, then run start → health → stop.

    Returns a ValidationResult; never raises on subprocess failure. Only raises
    on programmer errors (e.g. negative timeout). Caller is responsible for
    ensuring stop runs even on health failure — this function always invokes
    stop, even after a failed start/health, to leave the container clean.

    ``timeout_seconds`` is the default per-phase timeout; individual phases can
    be tuned via ``start_timeout_seconds`` / ``health_timeout_seconds`` /
    ``stop_timeout_seconds`` (e.g. give start a longer budget than the health
    probe, which should be near-instant).
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    start_to = start_timeout_seconds if start_timeout_seconds is not None else timeout_seconds
    health_to = health_timeout_seconds if health_timeout_seconds is not None else timeout_seconds
    stop_to = stop_timeout_seconds if stop_timeout_seconds is not None else timeout_seconds
    for label, value in (("start", start_to), ("health", health_to), ("stop", stop_to)):
        if value <= 0:
            raise ValueError(f"{label}_timeout_seconds must be positive")

    cleanup_dir = working_dir is None
    workdir = working_dir or Path(tempfile.mkdtemp(prefix="coordinare-services-validate-"))

    try:
        start_path = workdir / "services-start.sh"
        stop_path = workdir / "services-stop.sh"
        health_path = workdir / "services-health.sh"
        for path, body in (
            (start_path, scripts.start),
            (stop_path, scripts.stop),
            (health_path, scripts.health),
        ):
            path.write_text(body)
            path.chmod(0o755)

        # Phase 1: start
        start_proc = _run(start_path, env=env, timeout=start_to)
        if start_proc.returncode != 0:
            _best_effort_stop(stop_path, env=env, timeout=stop_to)
            return ValidationResult(
                phase="start",
                stdout=start_proc.stdout,
                stderr=start_proc.stderr,
                ok=False,
                returncode=start_proc.returncode,
            )

        # Brief readiness pause before health probe. import lazily so tests can monkeypatch.
        import time

        time.sleep(health_delay_seconds)

        # Phase 2: health
        health_proc = _run(health_path, env=env, timeout=health_to)
        if health_proc.returncode != 0:
            _best_effort_stop(stop_path, env=env, timeout=stop_to)
            return ValidationResult(
                phase="health",
                stdout=health_proc.stdout,
                stderr=health_proc.stderr,
                ok=False,
                returncode=health_proc.returncode,
            )

        # Phase 3: stop
        stop_proc = _run(stop_path, env=env, timeout=stop_to)
        if stop_proc.returncode != 0:
            return ValidationResult(
                phase="stop",
                stdout=stop_proc.stdout,
                stderr=stop_proc.stderr,
                ok=False,
                returncode=stop_proc.returncode,
            )

        combined_stdout = "\n".join(
            [start_proc.stdout, health_proc.stdout, stop_proc.stdout]
        ).strip()
        return ValidationResult(
            phase=None, stdout=combined_stdout, stderr="", ok=True, returncode=0
        )
    finally:
        if cleanup_dir:
            _rm_tree(workdir)


def _run(
    script: Path, *, env: dict[str, str] | None, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=timeout,
        check=False,
    )


def _best_effort_stop(
    stop_path: Path, *, env: dict[str, str] | None, timeout: float
) -> None:
    # Stop is cleanup; failure here doesn't override the original validation failure.
    with contextlib.suppress(Exception):
        _run(stop_path, env=env, timeout=timeout)


def _rm_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
