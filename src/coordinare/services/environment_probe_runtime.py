"""Trusted stdlib-only probe supervisor sent to the disposable image interpreter."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import sys
import time


def expand(value: str) -> str:
    roots = {"repo": "/repo", "cache": "/cache", "scratch": "/scratch"}
    return re.sub(r"(?<!\$)\{(repo|cache|scratch)\}", lambda match: roots[match.group(1)], value)


async def execute(command: dict, seconds: float) -> dict:
    result = {"id": command["id"], "status": "execution_error", "exit_code": None, "output": "", "truncated": False}
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": "/scratch", "TMPDIR": "/scratch", "LANG": "C.UTF-8"}
    env.update({key: expand(value) for key, value in command.get("env", {}).items() if key not in {"BASH_ENV", "ENV"}})
    output = bytearray()
    process = None

    async def drain() -> None:
        assert process is not None and process.stdout is not None
        while chunk := await process.stdout.read(8192):
            room = max(0, 16384 - len(output))
            output.extend(chunk[:room])
            result["truncated"] |= len(chunk) > room

    try:
        process = await asyncio.create_subprocess_exec(*[expand(value) for value in command["argv"]],
            cwd=expand(command.get("cwd", "{repo}")), env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        reader = asyncio.create_task(drain())
        try:
            async with asyncio.timeout(min(seconds, command["timeout_seconds"])):
                await asyncio.gather(process.wait(), reader)
            result.update(status="completed", exit_code=process.returncode)
        except TimeoutError:
            result["status"] = "timed_out"
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await process.wait()
    except FileNotFoundError:
        result.update(status="completed", exit_code=127, output="executable or working directory unavailable")
    except OSError:
        result["output"] = "probe process could not execute"
    if output:
        result["output"] = output.decode("utf-8", errors="ignore")
    return result


async def main() -> None:
    data = json.load(sys.stdin)
    deadline = time.monotonic() + min(120, data["seconds"])
    results = []
    for command in data["commands"]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append({"id":command["id"], "status":"timed_out", "exit_code":None, "output":"", "truncated":False})
        else:
            results.append(await execute(command, remaining))
    sys.stdout.write(json.dumps(results) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
