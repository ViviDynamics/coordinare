"""DriverBackend — integrates the driver agent harness (Coordinare family) via its
machine contract v1 JSONL wire protocol.

#521, per ``specs/515-driver-integration-contract/contracts/coordinare-driver-contract.md``:

    driver run --yes --contract 1 --jsonl [--session|--resume] <prompt>

One card (or resume turn) is one ``driver run`` process. Stdout is one JSON
object per line: event types ``progress | tool_use | thinking | cost | error |
output`` (coordinare's ``BackendEventType`` verbatim) followed by a terminal
non-event ``result`` line. Exit codes are outcomes: ``done``/``blocked`` → 0,
turn ``error`` → 1, never-started → 2. Keys travel by env only; argv is
world-readable. The session file is written atomically per turn, so
SIGTERM-then-resume recovery works.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
import tempfile
import uuid
from collections import deque
from dataclasses import replace as _dc_replace

import psutil
import structlog

from performer.backends._clarifications import clarification_prompt_section
from performer.backends._relay_feedback import relay_feedback_prompt_section
from performer.backends._card_docs import (
    brief_prompt_sections,
    card_docs_prompt_section,
    qa_findings_prompt_section,
    repair_mandate_prompt_section,
    scanner_findings_prompt_section,
)
from performer.backends._env_policy import build_subprocess_env
from performer.backends._scope import scope_prompt_section
from performer.backends.base import BackendStatus
from performer.io_utils import iter_lines_chunked
from performer.models import DIAGNOSTIC_ROLE, BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200

#: The machine-contract version this adapter is written against (§6). Pinned so
#: a newer driver refuses at start (exit 2) instead of mid-session.
_CONTRACT_VERSION = "1"

#: C4: driver exits 2 when it never started — an adapter/configuration error that
#: must surface immediately (no turn retry).
_EXIT_CONFIG_ERROR = 2

_RESULT_FIELDS = (
    "session_id", "status", "questions", "usage", "stop_reason", "turns",
    "contract", "driver", "output", "error",
)


class DriverBackend:
    """Backend adapter that drives ``driver run`` in JSONL contract mode."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._status: BackendStatus = BackendStatus(state="working")
        self._event_buffer: deque[BackendEvent] = deque(maxlen=200)
        self._log_buffer: deque[str] = deque(maxlen=200)
        self._reader_task: asyncio.Task[None] | None = None
        self._env: dict[str, str] = {}
        self._cwd: str = "."
        self._session_path: pathlib.Path | None = None
        self._saw_result = False
        self._tuning: dict[str, str | int | float | None] = {}

    # ------------------------------------------------------------------
    # BackendAdapter protocol
    # ------------------------------------------------------------------

    async def start(
        self,
        stand: Stand,
        score: Score,
        *,
        model: str | None = None,
        effort: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        """Launch ``driver run --yes --contract 1 --jsonl`` (C1, C2, C3)."""
        env = build_subprocess_env(
            cache_env=stand.cache_env,
            git_env=stand.git_env,
            tool_env=score.tool_env,
        )
        self._cwd = str(stand.path)
        self._env = env
        self._tuning = {
            "model": model, "effort": effort,
            "temperature": temperature, "max_tokens": max_tokens,
        }
        self._session_path = pathlib.Path(tempfile.gettempdir()) / f"driver-{uuid.uuid4().hex}.json"
        cmd = self._build_command(score, resume=False)
        cmd.append(_build_task_prompt(score, stand_path=pathlib.Path(stand.path)))
        await self._spawn(cmd)

    def get_status(self) -> BackendStatus:
        return self._status

    def drain_events(self) -> list[BackendEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events

    async def relay_feedback(self, feedback: str) -> None:
        """§4 A2 — resume the live session with *feedback* when one exists.

        The session file is atomic per turn, so a SIGTERM-then-resume is safe
        (C3). With no live session the feedback is buffered: the next
        dispatch already carries it via ``score.relay_feedback``.
        """
        if self._proc is None or self._proc.returncode is not None:
            log.info("driver.feedback_buffered")
            return
        # Kill the old reader first: it must not observe the SIGTERM exit (or
        # run its post-EOF _handle_exit) against the reset state below.
        self._cancel_reader()
        await self._terminate()
        cmd = self._build_command(None, resume=True)
        cmd.append(feedback)
        self._tuning = {}  # the session keeps its original config on resume
        self._saw_result = False
        self._status = BackendStatus(state="working")
        await self._spawn(cmd)
        log.info("driver.resumed_for_feedback")

    async def stop(self) -> None:
        """Cancel the reader and kill the process group."""
        self._cancel_reader()
        await self._terminate()
        log.info("driver stopped")

    async def _cancel_reader(self) -> None:
        if self._reader_task is None or self._reader_task.done():
            return
        self._reader_task.cancel()
        try:
            await self._reader_task
        except (asyncio.CancelledError, Exception):
            log.debug("driver.reader_cancelled")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_command(
        self,
        score: Score | None,
        *,
        resume: bool,
    ) -> list[str]:
        cmd = ["driver", "run", "--yes", "--contract", _CONTRACT_VERSION, "--jsonl"]
        if resume:
            cmd += ["--resume", str(self._session_path)]
        else:
            cmd += ["--session", str(self._session_path)]
        # §3.5 proxy topology: DRIVER_BASE_URL is set at container level by the
        # dual-model proxy's one-env-var seam; driver speaks Chat Completions,
        # so a base URL implies the openai provider through the proxy.
        base_url = self._env.get("DRIVER_BASE_URL", "")
        if base_url:
            cmd += ["--provider", "openai", "--base-url", base_url]
        tuning = self._tuning
        model = tuning.get("model")
        effort = tuning.get("effort")
        temperature = tuning.get("temperature")
        max_tokens = tuning.get("max_tokens")
        if model:
            cmd += ["--model", str(model)]
        if temperature is not None:
            cmd += ["--temperature", str(temperature)]
        if max_tokens is not None:
            cmd += ["--max-tokens", str(max_tokens)]
        if effort:
            cmd += ["--effort", str(effort)]
        # §3.4: --system receives the persona text; --root confines driver's
        # file tools to the workspace (confinement, not a jail).
        if score is not None:
            if score.persona_instructions:
                cmd += ["--system", score.persona_instructions]
            cmd += ["--root", self._cwd]
        return cmd

    async def _spawn(self, cmd: list[str]) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._cwd,
            start_new_session=True,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name="driver-reader")

    async def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            async for raw in iter_lines_chunked(self._proc.stdout):
                line = raw.decode(errors="replace").strip()
                if line:
                    self._handle_line(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensive; surface as error
            log.warning("driver reader error", error=str(exc))
            if self._status.state == "working":
                self._status = BackendStatus(state="error", error_reason=str(exc))
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        if not self._saw_result:
            self._handle_exit(self._proc.returncode or 0)

    def _handle_line(self, line: str) -> None:
        """Translate one JSONL line into events / status (C5, C6)."""
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            self._log_buffer.append(line[:_MAX_TEXT])
            return
        if not isinstance(parsed, dict):
            return
        if parsed.get("type") == "result":
            self._handle_result(parsed)
            return
        self._handle_event(parsed)

    def _handle_event(self, event: dict[str, object]) -> None:
        """C5: ``BackendEvent`` passthrough; unrecognized → progress-equivalent."""
        raw_type = str(event.get("type", ""))
        text = str(event.get("text", ""))[:_MAX_TEXT] or raw_type
        try:
            etype = BackendEventType(raw_type)
        except ValueError:
            # An unrecognized event type is recorded, never dropped silently
            # and never fatal — the --contract preflight is the versioning gate.
            log.warning("driver.unrecognized_event_type", event_type=raw_type)
            etype = BackendEventType.progress
        self._event_buffer.append(BackendEvent(
            type=etype, text=text, detail=str(event.get("detail", "")),
        ))
        if etype == BackendEventType.cost:
            usage = event.get("usage")
            if isinstance(usage, dict):
                tokens = _usage_tokens(usage)
                if tokens:
                    self._status = _dc_replace(self._status, tokens_processed=tokens)

    def _handle_result(self, result: dict[str, object]) -> None:
        self._saw_result = True
        status = str(result.get("status", "error"))
        stop_reason = result.get("stop_reason")
        questions = result.get("questions")
        usage = result.get("usage")
        error = result.get("error")
        if stop_reason == "schema_violation":
            # C6: a schema violation is an error regardless of the reported
            # status — the run's output did not meet the wire contract.
            self._status = BackendStatus(
                state="error",
                error_reason=str(error) or 'driver stop_reason "schema_violation"',
                tokens_processed=_usage_tokens(usage),
            )
            return
        if status == "done":
            self._status = self._done_status(stop_reason, result, usage)
        elif status == "blocked":
            self._status = self._blocked_status(questions)
        else:
            reason = str(error) if error else f"driver status {status!r}"
            self._status = BackendStatus(state="error", error_reason=reason or None)

    def _done_status(
        self, stop_reason: object, result: dict[str, object], usage: object,
    ) -> BackendStatus:
        tokens = _usage_tokens(usage)
        output = _optional_text(result.get("output"))
        if stop_reason == "max_tokens":
            return BackendStatus(state="done", stop_reason="max_tokens", output=output, tokens_processed=tokens)
        return BackendStatus(
            state="done", output=output, tokens_processed=tokens,
            stop_reason=str(stop_reason) if stop_reason else None,
        )

    def _blocked_status(self, questions: object) -> BackendStatus:
        if not isinstance(questions, list) or not questions:
            # C6: empty questions on blocked is a contract violation, not a
            # valid state.
            return BackendStatus(
                state="error",
                error_reason="driver reported blocked with an empty questions list",
            )
        return BackendStatus(state="blocked", questions=[str(q) for q in questions])

    def _handle_exit(self, returncode: int) -> None:
        """C4: exit codes are outcomes, not failures."""
        if self._saw_result and returncode == 0:
            return  # the result line already decided the status
        if self._status.state != "working":
            return
        if returncode == _EXIT_CONFIG_ERROR:
            # Never-started: an adapter/configuration error — surface
            # immediately, do not retry the turn.
            log.error("driver.config_error")
            self._status = BackendStatus(
                state="error",
                error_reason="driver refused to start (exit 2): adapter/configuration error",
            )
        elif returncode != 0:
            tail = " | ".join(list(self._log_buffer)[-10:])
            self._status = BackendStatus(
                state="error",
                error_reason=(
                    f"driver exited rc={returncode}: {tail}" if tail
                    else f"driver exited rc={returncode}"
                ),
            )

    async def _terminate(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            pgid = os.getpgid(self._proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            log.debug("driver.killpg_term_skipped")
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                pgid = os.getpgid(self._proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    parent = psutil.Process(self._proc.pid)
                    for child in parent.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            log.debug("driver.kill_child_gone")
                        parent.kill()
                except psutil.NoSuchProcess:
                    log.debug("driver.kill_parent_gone")


def _build_task_prompt(score: Score, *, stand_path: pathlib.Path | None = None) -> str:
    """Task prompt for driver.

    Unlike pi, driver has a real --system slot, so the persona contract rides
    ``--system`` (§3.4) and the prompt body carries only the task itself.
    """
    parts: list[str] = [f"# Task: {score.title}", ""]
    if score.description:
        parts += [score.description, ""]
    parts += card_docs_prompt_section(score, stand_path)
    parts += qa_findings_prompt_section(score)
    parts += repair_mandate_prompt_section(score)
    parts += scanner_findings_prompt_section(score)
    parts += scope_prompt_section(score, "driver")
    parts += brief_prompt_sections(score)
    if score.acceptance_criteria:
        parts += ["## Acceptance Criteria", ""]
        parts.extend(f"- {c}" for c in score.acceptance_criteria)
    parts += clarification_prompt_section(score.clarifications)
    if score.pr_diff:
        parts += [
            "", "## PR Diff Under Review", "",
            "The unified diff below is the complete set of changes on this PR. "
            "Review it directly — do NOT report that no changes were supplied.",
            "", "```diff", score.pr_diff.rstrip("\n"), "```", "",
        ]
    parts += _feedback_sections(score)
    if score.role == DIAGNOSTIC_ROLE:
        parts += [
            "",
            "---",
            "This is a one-off diagnostic/benchmark task. Use any tools at your "
            "disposal to complete it. You do NOT need to commit, push, or open a "
            "pull request — just perform the task and report what you did.",
        ]
    else:
        parts += [
            "",
            "---",
            "Complete the task above. Commit your changes with a clear, descriptive commit message.",
            "Do not push or open a pull request — this will be handled automatically after you finish.",
        ]
    return "\n".join(parts).strip()


def _feedback_sections(score: Score) -> list[str]:
    """126: disputes of this stage's own prior findings — adjudicate, don't
    treat as fresh work items. Buffered human feedback rides the next turn."""
    parts: list[str] = []
    if getattr(score, "disputed_feedback", None):
        parts += [
            "", "## Disputed feedback to adjudicate", "",
            "The implementer disputed these items you previously raised. "
            "Re-examine each one: if the dispute is valid, do NOT re-raise it "
            "(pass if nothing else is wrong); if it is invalid, re-raise it "
            "explicitly in your verdict.",
            "",
        ]
        for _d in score.disputed_feedback:
            if isinstance(_d, dict) and _d.get("id"):
                parts.append(
                    f"- {_d['id']}: {_d.get('body', '')} — implementer says: "
                    f"{_d.get('reason', '')}",
                )
    parts += relay_feedback_prompt_section(score.relay_feedback)
    return parts


def _usage_tokens(usage: object) -> int | None:
    """C7: sum the provider's reported counts; omit when it gave nothing."""
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and total > 0:
        return int(total)
    counted = 0
    for field in ("input_tokens", "output_tokens"):
        value = usage.get(field)
        if isinstance(value, int) and value > 0:
            counted += value
    return counted or None


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
