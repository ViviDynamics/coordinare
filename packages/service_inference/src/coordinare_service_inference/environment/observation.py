"""Bounded supervisor; the host removes the whole container after each command."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from typing import Any

from coordinare_service_inference.environment.models import environment_order
from coordinare_service_inference.environment.runner import _expand

from .runtime_observation import observe_private


def command_environment(command: dict[str, Any], *, runtime_env: dict[str, str] | None = None,
                        roots: dict[str, str] | None = None) -> dict[str, str]:
    roots = roots or {"repo":"/repo", "cache":"/cache", "scratch":"/scratch"}
    env = {"PATH":os.environ.get("PATH", os.defpath), "HOME":roots['scratch'], "TMPDIR":roots['scratch'], "LANG":"C.UTF-8"}
    declared = command.get("env", {})
    for key in environment_order(declared):
        if key in {"BASH_ENV", "ENV"}:
            message = "probe shell startup hooks are forbidden"
            raise ValueError(message)
        env[key] = _expand(declared[key], roots, env)
    if runtime_env is not None:
        env.update({key: value for key, value in runtime_env.items() if key != "PATH"})
        env["PATH"] = runtime_env["COORDINARE_ENV_RUNTIME_BIN"]+os.pathsep+env["PATH"]
    return env


def capture(process: subprocess.Popen[bytes], deadline: float) -> tuple[bytes, bool, bool]:
    output = bytearray()
    truncated = False
    if process.stdout is None:
        return bytes(output), False, False
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return bytes(output), truncated, True
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                room = max(0, 16384 - len(output))
                output.extend(chunk[:room])
                truncated |= len(chunk) > room
    return bytes(output), truncated, False


def execute(command: dict[str, Any], seconds: float, *, runtime_env: dict[str, str] | None = None,
            roots: dict[str, str] | None = None) -> dict[str, Any]:
    result = {"id":command["id"], "status":"execution_error", "exit_code":None, "output":"", "truncated":False}
    roots = roots or {"repo":"/repo", "cache":"/cache", "scratch":"/scratch"}
    process = None
    try:
        env = command_environment(command, runtime_env=runtime_env, roots=roots)
        deadline = time.monotonic() + min(seconds, command["timeout_seconds"])
        process = subprocess.Popen(  # noqa: S603, RUF100 - isolated argv; S603 is enforced by the separate ratchet
            [_expand(value, roots, env) for value in command["argv"]],
            cwd=_expand(command.get("cwd", "{repo}"), roots, env), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        output, truncated, expired = capture(process, deadline)
        result.update(output=output.decode("utf-8", errors="ignore"), truncated=truncated)
        try:
            code = process.wait(timeout=max(0, deadline-time.monotonic()))
            result.update(status="completed", exit_code=code)
        except subprocess.TimeoutExpired:
            expired = True
        if expired:
            result.update(status="timed_out", exit_code=None)
    except FileNotFoundError:
        result.update(status="completed", exit_code=127, output="executable or working directory unavailable")
    except (OSError, ValueError):
        result["output"] = "probe process or environment could not be resolved"
    finally:
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            if process.stdout is not None:
                process.stdout.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
    return result


def main() -> None:
    data = json.load(sys.stdin)
    if data.get("runtime") is not None:
        results = asyncio.run(observe_private(data))
        sys.stdout.write(json.dumps(results)+"\n")
        return
    deadline = time.monotonic() + min(120, data["seconds"])
    results = []
    for command in data["commands"]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append({"id":command["id"], "status":"timed_out", "exit_code":None, "output":"", "truncated":False})
        else:
            results.append(execute(command, remaining, runtime_env=data.get("runtime_environment"), roots=data.get('roots')))
    sys.stdout.write(json.dumps(results) + "\n")


if __name__ == "__main__":
    main()
