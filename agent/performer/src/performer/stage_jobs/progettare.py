"""403/423: one Progettare request, inside an ephemeral performer container.

Steps: clone the repository's default branch with the workspace layer (in a directory of
the container's own, never the mounted request directory), run ``progettare blueprint``
with the request's own ``runs/`` as ``--runs-dir``, write ``runner.json``, and drop the
clone. Progettare writes the run's artifacts and ``run.json``; coordinare's provider reads
those. The container lives exactly as long as this run, so whether the run is in flight is
whether its container is.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
from pathlib import Path
from typing import Any

from performer.stage_checkout import clone_default_branch
from performer.stage_jobs import CREDENTIAL_ENV, hand_back, scratch_dir, write_json
from performer.workspace import WorkspaceSetupError

REQUEST_FILE = "request.json"
RESULT_FILE = "runner.json"
RUNS_DIR = "runs"
#: Bound on the clone, separate from the run's own timeout.
CLONE_TIMEOUT_SECONDS = 600
#: Environment every run gets, when the job has it. Anything else is named in pass_env.
#: 461: ``NODE_USE_ENV_PROXY`` with the proxy variables, so Node's own requests use them too.
BASE_ENV = (
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "NODE_USE_ENV_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO",
)
_TAIL = 2000


def _tail(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return (text or "").strip()[-_TAIL:]


def progettare_env(env: dict[str, str], spec: dict[str, Any], token: str) -> dict[str, str]:
    """What Progettare may see: the base names, the operator's ``pass_env`` names, and the
    token as ``GH_TOKEN`` (Progettare reads the issue with gh)."""
    names = [*BASE_ENV, *(spec.get("pass_env") or [])]
    result = {n: env[n] for n in names if n in env and n != CREDENTIAL_ENV}
    if token:
        result["GH_TOKEN"] = token
    host = str(spec.get("host") or "github.com")
    if host != "github.com":
        result["GH_HOST"] = host
    return result


async def _clone(spec: dict[str, Any], checkout: Path, token: str) -> dict[str, Any] | None:
    """Clone the default branch into ``checkout``; the failure as a result, or None."""
    try:
        await asyncio.wait_for(clone_default_branch(spec["clone_url"], checkout, token=token), CLONE_TIMEOUT_SECONDS)
    except WorkspaceSetupError as exc:
        return {"phase": "clone", "exit_code": None, "detail": _tail(str(exc))}
    except TimeoutError:
        return {"phase": "clone", "exit_code": None, "detail": f"git clone took longer than {CLONE_TIMEOUT_SECONDS}s"}
    return None


async def _plan(spec: dict[str, Any], directory: Path, checkout: Path, env: dict[str, str]) -> dict[str, Any]:
    config_file = spec.get("config_file")
    config = directory / str(config_file) if config_file else checkout / "progettare.yaml"
    if not config.is_file():
        where = spec.get("config_path") or "progettare.yaml on the repository's default branch"
        return {"phase": "config", "exit_code": None, "detail": f"no Progettare config: {where} does not exist"}
    runs = directory / RUNS_DIR
    runs.mkdir(exist_ok=True)
    timeout = float(spec["timeout_seconds"])
    with (directory / "stdout.txt").open("wb") as out, (directory / "stderr.txt").open("wb") as err:
        try:
            process = await asyncio.create_subprocess_exec(
                spec["command"], "blueprint", "--issue", spec["issue"], "--repo", str(checkout),
                "--config", str(config), "--runs-dir", str(runs),
                stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=err, cwd=directory,
                start_new_session=True, env=env,
            )
        except OSError as exc:
            return {"phase": "progettare", "exit_code": None, "detail": f"{type(exc).__name__}: {exc}"}
        try:
            code = await asyncio.wait_for(process.wait(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            return {
                "phase": "timeout", "exit_code": None,
                "detail": f"progettare ran longer than {spec['timeout_seconds']}s and was stopped",
            }
    stderr = _tail((directory / "stderr.txt").read_bytes())
    return {"phase": "progettare", "exit_code": code, "detail": stderr}


async def _run(directory: Path, token: str, env: dict[str, str]) -> dict[str, Any]:
    spec = json.loads((directory / REQUEST_FILE).read_text(encoding="utf-8"))
    scratch = scratch_dir()
    checkout = scratch / "checkout"
    try:
        return await _clone(spec, checkout, token) or await _plan(
            spec, directory, checkout, progettare_env(env, spec, token),
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_job(directory: Path, *, token: str = "", env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run the request in ``directory`` to its end and write ``runner.json``. Returns it."""
    source = dict(os.environ if env is None else env)
    source.pop(CREDENTIAL_ENV, None)
    directory = directory.expanduser().absolute()
    result: dict[str, Any] = {"phase": "runner", "exit_code": None, "detail": "the runner stopped unexpectedly"}
    try:
        result = asyncio.run(_run(directory, token, source))
    except Exception as exc:  # recorded, so the provider can say why
        result = {"phase": "runner", "exit_code": None, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        write_json(directory / RESULT_FILE, result)
        hand_back(directory)
    return result
