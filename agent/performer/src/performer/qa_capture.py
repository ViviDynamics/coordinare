"""Deterministic QA screenshot backstop (performer-owned, LLM-independent).

Small QA models drive a browser unreliably: they run the capture under the
env-cache's playwright-less `python3` (ModuleNotFoundError), reach for Selenium
(SessionNotCreatedError), or claim a `path_or_url` the capture never created.
When a visual change needs evidence and the app is already serving on the
configured port, coordinare captures the screenshot itself — the exact known-good
invocation (`/usr/local/bin/python3` + Playwright bundled Chromium) — so the
artifact does not depend on the model getting the browser right.

The app boot stays the QA agent's job (persona-guided); this only guarantees the
CAPTURE once something is serving, and NEVER returns a path it did not verify on
disk.
"""
from __future__ import annotations

import json
import re
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable

import structlog

log = structlog.get_logger(__name__)

# Shell metacharacters that mean "this is a script, not a single command".
_SHELL_OPS = re.compile(r"&&|\|\||[|;&`><(){}$\n]")

# The env-cache prepends its own playwright-less python3 onto PATH, so the
# capture MUST use the image's system interpreter explicitly.
SYSTEM_PYTHON = "/usr/local/bin/python3"

_CAPTURE_SCRIPT = (
    "import sys\n"
    "from playwright.sync_api import sync_playwright\n"
    "url, out = sys.argv[1], sys.argv[2]\n"
    "with sync_playwright() as p:\n"
    "    b = p.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])\n"
    "    pg = b.new_page()\n"
    "    pg.goto(url, timeout=20000, wait_until='load')\n"
    "    pg.screenshot(path=out, full_page=True)\n"
    "    b.close()\n"
    "print('captured', out)\n"
)


def _port_serving(host: str, port: str, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def app_base_url(env: dict[str, str]) -> str | None:
    """Loopback URL the app is expected to serve on, from PORT/PROTOCOL, or None."""
    port = str(env.get("PORT") or "").strip()
    if not port:
        return None
    proto = str(env.get("PROTOCOL") or "http").strip() or "http"
    return f"{proto}://127.0.0.1:{port}/"


def capture_app_screenshot(
    *,
    env: dict[str, str],
    out_path: str = "/tmp/qa_screenshot.png",
    python_bin: str = SYSTEM_PYTHON,
    timeout: int = 60,
    port_check: Callable[[str, str], bool] = _port_serving,
    runner: Callable[..., object] = subprocess.run,
) -> str | None:
    """Capture a screenshot of the running app; return the verified path or None.

    Deterministic + safe: returns None (no capture) when PORT is unset or the app
    is not serving, and only returns a path that actually exists and is non-empty
    on disk — never a claimed-but-absent artifact.
    """
    port = str(env.get("PORT") or "").strip()
    url = app_base_url(env)
    if not port or not url:
        return None
    if not port_check("127.0.0.1", port):
        log.info("qa_capture.app_not_serving", port=port)
        return None
    out = Path(out_path)
    try:
        out.unlink()
    except OSError:
        pass
    try:
        runner(
            [python_bin, "-c", _CAPTURE_SCRIPT, url, out_path],
            capture_output=True,
            timeout=timeout,
        )
    except Exception as exc:  # FileNotFoundError (no python), TimeoutExpired, etc.
        log.warning("qa_capture.failed", error_type=type(exc).__name__)
        return None
    if out.is_file() and out.stat().st_size > 0:
        log.info("qa_capture.captured", path=out_path, bytes=out.stat().st_size, url=url)
        return out_path
    log.info("qa_capture.no_file_produced", path=out_path)
    return None


def infer_app_start_command(workspace: Path, env: dict[str, str]) -> list[str] | None:
    """Best-effort project-type inference for the web-app start command, or None.

    Deliberately conservative: recognizes the common web frameworks and returns
    None for anything unrecognized rather than guessing (a wrong command wastes
    the boot window and produces nothing). Boots on 127.0.0.1:$PORT so the
    loopback capture can reach it.
    """
    ws = Path(workspace)
    port = str(env.get("PORT") or "").strip()

    # Rails — bin/rails is the reliable signal (Gemfile alone can be a gem/lib).
    if (ws / "bin" / "rails").is_file():
        rails_env = str(env.get("RAILS_ENV") or "test").strip() or "test"
        return ["bin/rails", "server", "-b", "127.0.0.1", "-p", port, "-e", rails_env]

    # Django.
    if (ws / "manage.py").is_file():
        return ["python", "manage.py", "runserver", f"127.0.0.1:{port}"]

    # Node — prefer a `dev` script, then `start`.
    pkg = ws / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {}) or {}
        except (OSError, ValueError):
            scripts = {}
        for name in ("dev", "start"):
            if name in scripts:
                return ["npm", "run", name]

    return None


def _split_env_prefix(command: str) -> tuple[list[str], dict[str, str]]:
    """Split `VAR=val prog --flag` into (argv, extra_env).

    The spawner takes an argv list; `RAILS_ENV=test bin/rails server` split
    naively runs 'RAILS_ENV=test' as argv[0]. The assignment is a child
    process env var, not a program name.
    """
    parts = shlex.split(command)
    extra: dict[str, str] = {}
    while parts and "=" in parts[0]:
        key, _, value = parts[0].partition("=")
        if not key:
            break
        extra[key] = value
        parts = parts[1:]
    return parts, extra


def resolve_start_command(
    workspace: Path,
    env: dict[str, str],
    shape: object | None = None,
) -> tuple[list[str], dict[str, str]] | None:
    """The command to boot the app with, as (argv, extra_env) — or None.

    Order: the operator's QA_APP_START_COMMAND override, then the project
    shape's reading, then the framework inference — the shape layer sits in
    front of the inference, not instead of it (411 AC5).
    """
    raw = str(env.get("QA_APP_START_COMMAND") or "").strip()
    if not raw and shape is not None:
        raw = (getattr(shape, "start_command", "") or "").strip()
    if raw:
        try:
            argv, extra_env = _split_env_prefix(raw)
        except ValueError:
            # An unparseable command (unmatched quote) must not abort the
            # whole capture: fall through to framework inference the way a
            # missing command would (411 round-six review).
            log.warning("qa_capture.start_command_unparseable")
            argv, extra_env = [], {}
        if argv:
            return argv, extra_env
    inferred = infer_app_start_command(Path(workspace), env)
    if inferred:
        return inferred, {}
    return None


def _terminate(proc: object) -> None:
    """Stop a launched app server, escalating to SIGKILL if it ignores SIGTERM.

    A graceful SIGTERM + bounded wait is tried first; if the process is stubborn
    (e.g. Puma/Rails that takes >10s to drain, or ignores SIGTERM), the wait
    times out and we MUST escalate to kill() and reap it — otherwise the server
    leaks and keeps holding PORT across QA runs.
    """
    try:
        proc.terminate()  # type: ignore[attr-defined]
    except Exception:  # already gone / no such method
        return
    try:
        proc.wait(timeout=10)  # type: ignore[attr-defined]
        return
    except Exception:  # TimeoutExpired (or similar) — graceful stop failed
        log.warning("qa_capture.terminate_timeout_killing")
    try:
        proc.kill()  # type: ignore[attr-defined]
        proc.wait(timeout=10)  # type: ignore[attr-defined]
    except Exception:
        pass


def boot_and_capture_app_screenshot(
    *,
    env: dict[str, str],
    workspace: Path,
    out_path: str = "/tmp/qa_screenshot.png",
    python_bin: str = SYSTEM_PYTHON,
    timeout: int = 60,
    boot_timeout: float = 60.0,
    poll_interval: float = 1.0,
    port_check: Callable[[str, str], bool] = _port_serving,
    runner: Callable[..., object] = subprocess.run,
    spawner: Callable[..., object] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
    shape: object | None = None,
) -> str | None:
    """Ensure the app is serving (booting it if needed), then capture — or None.

    If the app is already up on PORT, captures directly. Otherwise resolves the
    start command (operator override → shape → inference, via
    ``resolve_start_command``), launches it in the activated env (cwd=workspace),
    polls until it serves (bounded by ``boot_timeout``), captures, and always
    tears the launched server back down. Returns None — never a fabricated path —
    when PORT is unset, no command can be resolved, the server never comes up, or
    the capture produces no file.
    """
    port = str(env.get("PORT") or "").strip()
    if not port:
        return None

    resolved = resolve_start_command(Path(workspace), env, shape)
    # The command may override PORT itself (`PORT=9000 ...`): the effective
    # spawn env, not the outer env, decides which port to probe and capture
    # against (411 round-six review).
    spawn_env = {**env, **resolved[1]} if resolved else env
    effective_port = str(spawn_env.get("PORT") or "").strip() or port

    if port_check("127.0.0.1", effective_port):
        return capture_app_screenshot(
            env=spawn_env, out_path=out_path, python_bin=python_bin, timeout=timeout,
            port_check=port_check, runner=runner,
        )
    if not resolved:
        log.info("qa_capture.no_start_command_inferred", workspace=str(workspace))
        return None
    cmd, extra_env = resolved

    # The operator override is a trusted human command and may legitimately
    # use shell semantics (`cd web && npm start`); AppBoot._default_spawn
    # grants it a shell at boot, so the fallback capture must not lose those
    # semantics here (411 round-six review). But the shell branch spawns the
    # override STRING itself, so use_shell is only correct when the override
    # actually parsed: an unparseable override falls back to framework
    # inference, and recomputing use_shell from the raw override would spawn
    # the broken string instead of the inferred command (411 round-eight
    # review).
    override = str(env.get("QA_APP_START_COMMAND") or "").strip()
    from_override: list[str] | None = None
    if override:
        try:
            from_override, _extra = _split_env_prefix(override)
        except ValueError:
            pass
    use_shell = cmd == from_override and bool(_SHELL_OPS.search(override))
    log.info("qa_capture.booting_app", cmd=cmd, port=effective_port, shell=use_shell)
    try:
        if use_shell:
            proc = spawner(
                override,
                shell=True, cwd=str(workspace), env=spawn_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            proc = spawner(
                cmd, cwd=str(workspace), env=spawn_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except Exception as exc:
        log.warning("qa_capture.boot_spawn_failed", error_type=type(exc).__name__)
        return None

    try:
        waited = 0.0
        while waited < boot_timeout:
            if port_check("127.0.0.1", effective_port):
                break
            sleep(poll_interval)
            waited += poll_interval
        if not port_check("127.0.0.1", effective_port):
            log.warning("qa_capture.app_boot_timeout", port=effective_port, waited=waited)
            return None
        log.info("qa_capture.app_booted", port=effective_port, waited=waited)
        return capture_app_screenshot(
            env=spawn_env, out_path=out_path, python_bin=python_bin, timeout=timeout,
            port_check=port_check, runner=runner,
        )
    finally:
        _terminate(proc)
