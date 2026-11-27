"""403: the detached runner of one Progettare request.

Started by :class:`coordinare.services.progettare.ProgettareProvider` as
``python -m coordinare.services.progettare_runner <request_dir> <lock_fd>`` in its own
session, so a restart of the daemon leaves it running. It inherits the request's lock
(an ``flock`` the provider took before starting it) and hands the same descriptor to
Progettare, so the lock is held for as long as either lives: a held lock means the run
is in flight, a free one means it is over.

Steps: clone the repository's default branch (the token goes to git through the
environment, never the command line), run ``progettare blueprint`` with the run's own
``--runs-dir``, write ``runner.json``, and remove the clone. Progettare writes the run's
artifacts and ``run.json``; the provider reads those.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any

#: Bound on the clone, separate from the run's own timeout.
CLONE_TIMEOUT_SECONDS = 600
_TAIL = 2000


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: a reader sees the old file or the new one, never half."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    Path(handle.name).replace(path)


def _tail(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return (text or "").strip()[-_TAIL:]


_PYTHON_ENV = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")


def without_coordinare_python(env: dict[str, str]) -> dict[str, str]:
    """``env`` without coordinare's own Python: its import path, its virtualenv, and that
    virtualenv's ``bin`` on PATH. A Python-based CLI in its own environment (Progettare)
    then never imports coordinare's packages or resolves ``python3`` to its interpreter."""
    venv = env.get("VIRTUAL_ENV", "").rstrip("/")
    cleaned = {k: v for k, v in env.items() if k not in _PYTHON_ENV}
    if venv and "PATH" in cleaned:
        bins = {f"{venv}/bin", str(Path(sys.executable).parent)}
        cleaned["PATH"] = os.pathsep.join(p for p in cleaned["PATH"].split(os.pathsep) if p.rstrip("/") not in bins)
    return cleaned


def _git_env(host: str) -> dict[str, str]:
    """The environment git clones with: the token as an extra header, no prompts."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    token = env.get("GH_TOKEN", "")
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = f"http.https://{host}/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    return env


async def _clone(spec: dict[str, Any], checkout: Path) -> dict[str, Any] | None:
    """Clone the default branch into ``checkout``; the failure as a result, or None."""
    shutil.rmtree(checkout, ignore_errors=True)
    git = shutil.which("git")
    if git is None:
        return {"phase": "clone", "exit_code": None, "detail": "git is not on PATH"}
    try:
        process = await asyncio.create_subprocess_exec(
            git, "clone", "--quiet", "--depth", "1", "--no-tags", spec["clone_url"], str(checkout),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, env=_git_env(str(spec.get("host") or "github.com")),
        )
    except OSError as exc:
        return {"phase": "clone", "exit_code": None, "detail": f"{type(exc).__name__}: {exc}"}
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), CLONE_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        return {"phase": "clone", "exit_code": None, "detail": f"git clone took longer than {CLONE_TIMEOUT_SECONDS}s"}
    if process.returncode != 0:
        return {"phase": "clone", "exit_code": process.returncode, "detail": _tail(stderr)}
    return None


async def _plan(spec: dict[str, Any], directory: Path, checkout: Path, lock_fd: int) -> dict[str, Any]:
    config = Path(spec["config_path"]) if spec.get("config_path") else checkout / "progettare.yaml"
    if not config.is_file():
        where = spec.get("config_path") or "progettare.yaml on the repository's default branch"
        return {"phase": "config", "exit_code": None, "detail": f"no Progettare config: {where} does not exist"}
    runs = directory / "runs"
    runs.mkdir(exist_ok=True)
    timeout = float(spec["timeout_seconds"])
    with (directory / "stdout.txt").open("wb") as out, (directory / "stderr.txt").open("wb") as err:
        try:
            # Progettare holds the lock too, so a run that outlives this runner still
            # reads as in flight and is never started a second time.
            process = await asyncio.create_subprocess_exec(
                spec["command"], "blueprint", "--issue", spec["issue"], "--repo", str(checkout),
                "--config", str(config), "--runs-dir", str(runs),
                stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=err, cwd=directory,
                pass_fds=(lock_fd,), start_new_session=True, env=without_coordinare_python(dict(os.environ)),
            )
        except OSError as exc:
            return {"phase": "progettare", "exit_code": None, "detail": f"{type(exc).__name__}: {exc}"}
        try:
            code = await asyncio.wait_for(process.wait(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            return {
                "phase": "timeout", "exit_code": None,
                "detail": f"progettare ran longer than {spec['timeout_seconds']}s and was stopped",
            }
    stderr = _tail((directory / "stderr.txt").read_bytes())
    return {"phase": "progettare", "exit_code": code, "detail": stderr}


async def _run(directory: Path, lock_fd: int) -> None:
    spec = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    checkout = directory / "checkout"
    result: dict[str, Any] = {"phase": "runner", "exit_code": None, "detail": "the runner stopped unexpectedly"}
    try:
        result = await _clone(spec, checkout) or await _plan(spec, directory, checkout, lock_fd)
    except Exception as exc:  # recorded, so the provider can say why
        result = {"phase": "runner", "exit_code": None, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
        write_json(directory / "runner.json", result)


def main(argv: list[str] | None = None) -> int:
    try:
        directory, lock_fd = sys.argv[1:] if argv is None else argv
    except ValueError:
        sys.stderr.write("usage: progettare_runner <request_dir> <lock_fd>\n")
        return 2
    asyncio.run(_run(Path(directory), int(lock_fd)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
