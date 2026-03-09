"""OpenCodeAdapter — integrates opencode via its ACP (nd-JSON) protocol."""
from __future__ import annotations

import asyncio
import json
import os
import signal

import psutil
import structlog

from performer.backends.base import BackendStatus
from performer.models import Score, Stand

log = structlog.get_logger(__name__)


class OpenCodeAdapter:
    """Backend adapter that drives ``opencode acp`` over stdin/stdout nd-JSON.

    Lifecycle:
        start() → launches subprocess, sends initial task message, begins reader task
        get_status() → returns current BackendStatus (non-blocking)
        relay_feedback() → writes nd-JSON feedback message to subprocess stdin
        stop() → kills process group; waits for subprocess to exit
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._status: BackendStatus = BackendStatus(state="working")
        self._reader_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # BackendAdapter protocol
    # ------------------------------------------------------------------

    async def start(self, stand: Stand, score: Score) -> None:
        """Launch ``opencode acp`` in *stand.path* and send the initial task."""
        self._proc = await asyncio.create_subprocess_exec(
            "opencode", "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout to prevent pipe-buffer stall
            cwd=str(stand.path),
            start_new_session=True,
        )
        initial_message = {
            "type": "message.create",
            "parts": [{"type": "text", "text": _build_task_prompt(score)}],
        }
        await self._write_nd_json(initial_message)
        self._reader_task = asyncio.create_task(
            self._event_reader_loop(), name="opencode-reader"
        )
        log.info("opencode acp started", pid=self._proc.pid)

    def get_status(self) -> BackendStatus:
        """Return the current backend status (non-blocking)."""
        return self._status

    async def relay_feedback(self, feedback: str) -> None:
        """Write a follow-up message to the running opencode process."""
        message = {
            "type": "message.create",
            "parts": [{"type": "text", "text": feedback}],
        }
        await self._write_nd_json(message)
        # After receiving feedback, the backend transitions back to working
        self._status = BackendStatus(state="working")

    async def stop(self) -> None:
        """Kill the opencode process group and wait for it to exit."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._proc is not None and self._proc.returncode is None:
            try:
                pgid = os.getpgid(self._proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                # Fallback: use psutil to kill the tree
                try:
                    parent = psutil.Process(self._proc.pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        log.info("opencode acp stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _write_nd_json(self, obj: dict) -> None:  # type: ignore[type-arg]
        if self._proc is None or self._proc.stdin is None:
            return
        line = json.dumps(obj) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()

    async def _event_reader_loop(self) -> None:
        """Read nd-JSON events from stdout and update internal status."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            async for raw_line in self._proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle_event(event)
                if self._status.state in ("done", "error"):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("event reader error", error=str(exc))
            self._status = BackendStatus(state="error", error_reason=str(exc))
        finally:
            # If the subprocess exited without emitting a terminal event, transition
            # away from "working" so the performer doesn't poll forever.
            if self._status.state == "working" and self._proc is not None:
                try:
                    if self._proc.returncode is None:
                        await self._proc.wait()
                    rc = self._proc.returncode
                    if rc == 0:
                        self._status = BackendStatus(state="done")
                    else:
                        self._status = BackendStatus(
                            state="error",
                            error_reason=(
                                f"opencode subprocess exited with code {rc} "
                                "without emitting a terminal event"
                            ),
                        )
                except Exception as exc:  # pragma: no cover
                    self._status = BackendStatus(state="error", error_reason=str(exc))

    def _handle_event(self, event: dict) -> None:  # type: ignore[type-arg]
        event_type = event.get("type", "")

        if event_type == "session.idle":
            self._status = BackendStatus(state="done")

        elif event_type == "session.error":
            reason = event.get("error", {}).get("message", str(event))
            self._status = BackendStatus(state="error", error_reason=reason)

        elif event_type in ("session.message", "message.create"):
            # Blocked/question detection: treat message parts with type "question"
            # or "needsInput" as signals that the backend needs human input.
            # This maps to opencode ACP's best-known part types; update if the
            # ACP spec evolves to use a different event shape.
            parts = event.get("parts", [])
            questions = [
                p.get("text", "")
                for p in parts
                if p.get("type") in ("question", "needsInput") and p.get("text")
            ]
            if questions:
                self._status = BackendStatus(state="blocked", questions=questions)

        elif event_type == "message.part.updated":
            part = event.get("part", {})
            if part.get("type") == "text":
                self._status = BackendStatus(
                    state="working", progress=part.get("text", "")[:200]
                )
        # All other event types are no-ops (keep current state)


def _build_task_prompt(score: Score) -> str:
    """Construct the task description sent to opencode as the initial message."""
    parts = [f"# Task: {score.title}", ""]
    if score.description:
        parts += [score.description, ""]
    if score.acceptance_criteria:
        parts += ["## Acceptance Criteria", ""]
        parts.extend(f"- {c}" for c in score.acceptance_criteria)
    return "\n".join(parts)
