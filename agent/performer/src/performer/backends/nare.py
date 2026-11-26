"""NareBackend: runs the nare agent harness through its machine contract.

#360. nare (github.com/ViviDynamics/nare) is driven as a subprocess:

    nare run --yes --contract 3 --jsonl --session PATH --prompt-file FILE \
        [--append-system-file FILE] --root WORKSPACE [provider and tuning flags]

One card, or one resume turn, is one ``nare run`` process. Stdout is one JSON
object per line: events ``progress | tool_use | thinking | cost | error |
output``, each with ``type``, ``text``, ``detail`` and ``timestamp``, followed by
a terminal ``result`` line. Exit codes are outcomes: ``done`` and ``blocked`` exit
0, a run that started and failed exits 1, a run that never started exits 2 with
nothing on stdout. Provider keys travel by env only, and the task and persona
travel by file, because argv is world-readable and capped per argument.

The contract this adapter reads is nare's ``docs/contract.md`` at the release the
performer base image pins (``NARE_VERSION`` in ``agent/performer/Dockerfile.base``).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import signal
import tempfile
import uuid
from collections import deque
from dataclasses import replace as _dc_replace
from typing import Any

import psutil
import structlog

from performer import transcript
from performer.backends._card_docs import (
    brief_prompt_sections,
    card_docs_prompt_section,
    qa_findings_prompt_section,
    repair_mandate_prompt_section,
    scanner_findings_prompt_section,
)
from performer.backends._clarifications import clarification_prompt_section
from performer.backends._env_policy import build_subprocess_env
from performer.backends._relay_feedback import relay_feedback_prompt_section
from performer.backends._role_contract import role_closing_section
from performer.backends._scope import scope_prompt_section
from performer.backends._tool_summary import primary_argument
from performer.backends.base import BackendStatus
from performer.io_utils import iter_lines_chunked
from performer.models import BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200

#: The binary the performer image installs.
NARE_BINARY = "nare"

#: The machine-contract version this adapter reads. nare refuses any other
#: number at start (exit 2, nothing on stdout), so a newer nare fails the first
#: run loudly instead of being mis-read.
CONTRACT_VERSION = "3"

#: A run that never started: a bad invocation, an unreadable session file or a
#: refused contract. A configuration error, not a turn to retry.
_EXIT_CONFIG_ERROR = 2

#: Providers whose rail accepts ``--temperature``. The anthropic and bedrock
#: rails refuse it at start.
_TEMPERATURE_PROVIDERS = frozenset({"openai"})

#: Endpoint kinds that are nare rails of the same name.
_NATIVE_RAILS = frozenset({"anthropic", "openai"})

#: nare's openai rail refuses to start without OPENAI_API_KEY, and a keyless
#: gateway (Ollama, a local vLLM) ignores whatever it is sent.
_KEYLESS_GATEWAY_KEY = "unused"

#: nare's built-in tool names, mapped to the names the 148 activity-feed summary knows.
_SUMMARY_TOOL_NAMES = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "search": "Grep",
}

_NARE_CAPABILITY = (
    "nare --jsonl carries full text on each event line. A tool call is a tool_use "
    "event with its arguments, and its result is a progress event with the outcome "
    "and size but not the content, so tool_result records carry no output. "
    "assistant_text comes from output events; usage comes from cost events (per "
    "request) and the result line (run total)."
)


def _count(value: object) -> int | None:
    """A non-negative token count as nare reported it, else None."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


class NareBackend:
    """Backend adapter that drives ``nare run`` in JSONL contract mode."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._status: BackendStatus = BackendStatus(state="working")
        self._event_buffer: deque[BackendEvent] = deque(maxlen=200)
        self._log_buffer: deque[str] = deque(maxlen=200)
        self._reader_task: asyncio.Task[None] | None = None
        self._env: dict[str, str] = {}
        self._cwd: str = "."
        self._run_dir: pathlib.Path | None = None
        self._session_path: pathlib.Path | None = None
        self._saw_result = False
        self._tuning: dict[str, str | int | float | None] = {}
        self._max_tool_calls: int | None = None
        self._tool_calls_used = 0
        self._tokens_seen = 0
        self._tokens_before_resume = 0
        self._endpoint_kind = ""
        self._endpoint_base_url = ""
        self._prompt_seq = 0

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
        """Launch a fresh ``nare run`` session for *score*."""
        env = build_subprocess_env(
            cache_env=stand.cache_env,
            git_env=stand.git_env,
            tool_env=score.tool_env,
        )
        self._cwd = str(stand.path)
        self._env = env
        self._endpoint_kind = str(getattr(score, "endpoint_kind", "") or "").strip().lower()
        self._endpoint_base_url = str(getattr(score, "base_url", "") or "").strip()
        if self._base_url() and not env.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = _KEYLESS_GATEWAY_KEY
        self._tuning = {
            "model": model, "effort": effort,
            "temperature": temperature, "max_tokens": max_tokens,
        }
        self._max_tool_calls = getattr(score, "max_tool_calls", None)
        self._tool_calls_used = 0
        # 223: a restart after a format failure reuses this instance. It gets a
        # new session file, and get_status() must not return the previous run.
        self._remove_run_dir()
        self._run_dir = pathlib.Path(tempfile.mkdtemp(prefix="nare-"))
        self._session_path = self._run_dir / f"session-{uuid.uuid4().hex}.json"
        self._status = BackendStatus(state="working")
        self._saw_result = False
        self._tokens_seen = 0
        self._tokens_before_resume = 0
        task_text = _build_task_prompt(score, stand_path=pathlib.Path(stand.path))
        cmd = self._build_command(task_text, resume=False)
        if score.persona_instructions:
            persona = self._write_file("persona.md", score.persona_instructions)
            cmd += ["--append-system-file", str(persona)]
        transcript.emit(
            "capability",
            text=_NARE_CAPABILITY,
            input_data={
                "unavailable": ["tool_result_content"],
                "provided": [
                    "user", "system", "assistant_text", "tool_call", "tool_result",
                    "thinking", "usage", "error", "result",
                ],
            },
        )
        transcript.emit("user", text=task_text)
        await self._spawn(cmd)

    def get_status(self) -> BackendStatus:
        return self._status

    def drain_events(self) -> list[BackendEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events

    async def relay_feedback(self, feedback: str) -> None:
        """Resume the live session with *feedback*.

        nare saves the session atomically and writes it on SIGTERM, so stopping
        the run and resuming it with the feedback as the next prompt loses no
        completed turn. With no live session the feedback is buffered: the next
        dispatch already carries it in ``score.relay_feedback``.
        """
        if self._proc is None or self._proc.returncode is not None:
            log.info("nare.feedback_buffered")
            return
        # Stop the old reader first, so it never sees the SIGTERM exit or runs
        # its end-of-stream handling against the reset state below.
        await self._cancel_reader()
        await self._terminate()
        # A resumed process reports its own usage; keep what came before it.
        self._tokens_before_resume = self._tokens_seen
        cmd = self._build_command(feedback, resume=True)
        transcript.emit("user", text=feedback)
        self._saw_result = False
        self._status = BackendStatus(state="working")
        await self._spawn(cmd)
        log.info("nare.resumed_for_feedback")

    async def stop(self) -> None:
        """Cancel the reader, stop the process group and remove the run's files."""
        await self._cancel_reader()
        await self._terminate()
        self._remove_run_dir()
        log.info("nare.stopped")

    def _remove_run_dir(self) -> None:
        """The persona, every prompt (whole PR diffs) and the session file. A
        long-lived performer would otherwise keep one directory per job."""
        if self._run_dir is not None:
            shutil.rmtree(self._run_dir, ignore_errors=True)
            self._run_dir = None

    async def _cancel_reader(self) -> None:
        if self._reader_task is None or self._reader_task.done():
            return
        self._reader_task.cancel()
        try:
            await self._reader_task
        except (asyncio.CancelledError, Exception):
            log.debug("nare.reader_cancelled")

    # ------------------------------------------------------------------
    # Command line
    # ------------------------------------------------------------------

    def _write_file(self, stem: str, text: str) -> pathlib.Path:
        if self._run_dir is None:
            self._run_dir = pathlib.Path(tempfile.mkdtemp(prefix="nare-"))
        path = self._run_dir / stem
        path.write_text(text, encoding="utf-8")
        return path

    def _base_url(self) -> str:
        """The proxy's loopback when it set one (NARE_BASE_URL, proxy/launch.py),
        else the role's self-hosted endpoint."""
        return self._env.get("NARE_BASE_URL", "").strip() or self._endpoint_base_url

    def _provider(self) -> str | None:
        """The rail. A base URL is always an OpenAI-compatible gateway (the
        dual-model proxy, LiteLLM, vLLM, Ollama), so it wins even over a native
        anthropic endpoint the proxy fronts. Then the endpoint's own kind, then
        an operator's NARE_PROVIDER, then nare's default (anthropic)."""
        if self._base_url():
            return "openai"
        if self._endpoint_kind in _NATIVE_RAILS:
            return self._endpoint_kind
        return self._env.get("NARE_PROVIDER", "").strip() or None

    def _build_command(self, prompt: str, *, resume: bool) -> list[str]:
        """The argv for one invocation. A resume repeats the run flags, since a
        nare session records the transcript and system prompt but not the
        provider, model or budgets."""
        self._prompt_seq += 1
        prompt_file = self._write_file(f"prompt-{self._prompt_seq}.md", prompt)
        cmd = [NARE_BINARY, "run", "--yes", "--contract", CONTRACT_VERSION, "--jsonl"]
        if resume:
            cmd += ["--resume", str(self._session_path)]
        else:
            cmd += ["--session", str(self._session_path)]
        cmd += ["--prompt-file", str(prompt_file), "--root", self._cwd]
        provider = self._provider()
        if provider:
            cmd += ["--provider", provider]
        base_url = self._base_url()
        if base_url:
            cmd += ["--base-url", base_url]
        cmd += self._tuning_flags(provider)
        cmd += self._tool_cap_flags()
        return cmd

    def _tuning_flags(self, provider: str | None) -> list[str]:
        tuning = self._tuning
        flags: list[str] = []
        model = tuning.get("model")
        if model:
            flags += ["--model", str(model)]
        temperature = tuning.get("temperature")
        if temperature is not None:
            if provider in _TEMPERATURE_PROVIDERS:
                flags += ["--temperature", str(temperature)]
            else:
                log.info("nare.temperature_dropped", provider=provider or "anthropic")
        max_tokens = tuning.get("max_tokens")
        if max_tokens is not None:
            flags += ["--max-tokens", str(max_tokens)]
        effort = tuning.get("effort")
        if effort:
            flags += ["--effort", str(effort)]
        return flags

    def _tool_cap_flags(self) -> list[str]:
        """nare counts --max-tool-calls per process, so a resume gets what is left."""
        if self._max_tool_calls is None:
            return []
        remaining = self._max_tool_calls - self._tool_calls_used
        return ["--max-tool-calls", str(remaining)] if remaining > 0 else ["--tools", "none"]

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
        self._reader_task = asyncio.create_task(self._read_loop(), name="nare-reader")

    # ------------------------------------------------------------------
    # Stream
    # ------------------------------------------------------------------

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
            log.warning("nare.reader_error", error=str(exc))
            if self._status.state == "working":
                self._status = BackendStatus(state="error", error_reason=str(exc))
            if not self._saw_result:
                self._record_outcome(None)
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except TimeoutError:
            # stdout closed but the process lives on: it cannot report anything now.
            await self._terminate()
        if not self._saw_result:
            self._handle_exit(self._proc.returncode or 0)
            self._record_outcome(None)

    def _handle_line(self, line: str) -> None:
        """Translate one JSONL line into events and status."""
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            # stderr is merged into stdout; nare writes its diagnostics there.
            self._log_buffer.append(line[:_MAX_TEXT])
            transcript.emit("system", text=line, input_data={"raw_line": line})
            return
        if not isinstance(parsed, dict):
            return
        if parsed.get("type") == "result":
            self._handle_result(parsed)
            self._record_outcome(parsed)
            return
        transcript_seq = self._transcribe(parsed)
        self._handle_event(parsed, transcript_seq=transcript_seq)

    # ------------------------------------------------------------------
    # Transcript (spec 129). Records only; never changes status or events.
    # ------------------------------------------------------------------

    @transcript.best_effort
    def _transcribe(self, event: dict[str, Any]) -> int | None:
        """Record one event line with its full text, before the 200-character cut.

        Returns the seq of the record that describes the event (211).
        """
        raw_type = str(event.get("type", ""))
        text = str(event.get("text", ""))
        detail = event.get("detail")
        call_id = str(event.get("tool_use_id", "")) or None
        tool_result = _tool_result(detail)
        if raw_type == "tool_use":
            return transcript.emit(
                "tool_call", text=text, tool_name=text or "tool", call_id=call_id,
                input_data=detail,
            )
        if raw_type == "progress" and tool_result is not None:
            return transcript.emit(
                "tool_result", text=text, tool_name=str(detail.get("tool", "")) or None,
                call_id=call_id, input_data=detail, is_error=tool_result.get("is_error") is True,
            )
        if raw_type == "output":
            return transcript.emit("assistant_text", text=text, input_data=event)
        if raw_type == "thinking":
            return transcript.emit("thinking", text=text, input_data=event)
        if raw_type == "cost":
            return self._emit_usage(detail, "nare cost event: one model request", "message")
        if raw_type == "error":
            return transcript.emit("error", text=text, input_data=event)
        # Status progress and unrecognised types are not model text; they keep
        # their raw payload as system records.
        return transcript.emit("system", text=text, input_data=event)

    @transcript.best_effort
    def _record_outcome(self, raw: dict[str, Any] | None) -> None:
        """The final result record. ``raw`` is the result line, or None when the
        process ended without one."""
        if raw is not None:
            self._emit_usage(raw.get("usage"), "run total from the nare result line", "run")
        state = self._status.state
        is_error = state == "error"
        input_data: dict[str, Any] = raw if raw is not None else {
            "source": "process exited without a result line",
            "state": state,
            "stop_reason": self._status.stop_reason,
        }
        transcript.emit(
            "result",
            text=self._status.error_reason if is_error else None,
            output_data=self._status.output,
            input_data=input_data,
            is_error=is_error,
        )

    @transcript.best_effort
    def _emit_usage(
        self,
        usage: object,
        note: str,
        scope: transcript.TranscriptScope | None,
    ) -> int | None:
        if not isinstance(usage, dict):
            return None
        counts = transcript.TranscriptUsage(
            input_tokens=_count(usage.get("input")),
            output_tokens=_count(usage.get("output")),
            cache_read=_count(usage.get("cache_read")),
            cache_write=_count(usage.get("cache_write")),
            model=str(self._tuning.get("model") or "") or None,
        )
        if counts.input_tokens is None and counts.output_tokens is None:
            return None
        seq: int | None = transcript.emit(
            "usage", text=note, input_data=usage, usage=counts, scope=scope,
        )
        return seq

    # ------------------------------------------------------------------
    # Events and status
    # ------------------------------------------------------------------

    def _handle_event(
        self, event: dict[str, object], *, transcript_seq: int | None = None,
    ) -> None:
        """Pass a nare event through as a ``BackendEvent``. An unrecognised type
        becomes ``output`` (178: a raw line, not narration), never an error: the
        ``--contract`` refusal is the versioning gate."""
        raw_type = str(event.get("type", ""))
        text = str(event.get("text", ""))[:_MAX_TEXT] or raw_type
        detail = event.get("detail")
        call_id = str(event.get("tool_use_id", ""))
        try:
            etype = BackendEventType(raw_type)
        except ValueError:
            log.warning("nare.unrecognized_event_type", event_type=raw_type)
            etype = BackendEventType.output
        tool_result = _tool_result(detail)
        if etype == BackendEventType.tool_use:
            self._tool_calls_used += 1
            tool = str(event.get("text", "")) or "tool"
            self._event_buffer.append(BackendEvent(
                type=etype, text=text, detail=tool, call_id=call_id, tool=tool,
                arg=primary_argument(_SUMMARY_TOOL_NAMES.get(tool, tool), detail),
                transcript_seq=transcript_seq,
            ))
            return
        if etype == BackendEventType.progress and tool_result is not None:
            status = "error" if tool_result.get("is_error") is True else "ok"
            self._event_buffer.append(BackendEvent(
                type=BackendEventType.tool_use, text=text, detail=call_id,
                call_id=call_id, status=status, transcript_seq=transcript_seq,
            ))
            return
        self._event_buffer.append(BackendEvent(
            type=etype, text=text, detail=_detail_text(detail), call_id=call_id,
            transcript_seq=transcript_seq,
        ))
        if etype == BackendEventType.cost and isinstance(detail, dict):
            tokens = _usage_tokens(detail)
            if tokens:
                self._tokens_seen += tokens
                self._status = _dc_replace(self._status, tokens_processed=self._tokens_seen)
        elif etype == BackendEventType.progress and self._status.state == "working":
            self._status = _dc_replace(self._status, progress=text)

    def _handle_result(self, result: dict[str, object]) -> None:
        self._saw_result = True
        status = str(result.get("status", "error"))
        stop_reason = result.get("stop_reason")
        usage = result.get("usage")
        error = result.get("error")
        run_tokens = _usage_tokens(usage)
        tokens = (self._tokens_before_resume + run_tokens) if run_tokens else (self._tokens_seen or None)
        if status == "done":
            self._status = BackendStatus(
                state="done",
                output=_final_text(result),
                tokens_processed=tokens,
                stop_reason=str(stop_reason) if stop_reason else None,
            )
        elif status == "blocked":
            self._status = self._blocked_status(result.get("questions"), tokens)
        else:
            reason = str(error) if error else f"nare status {status!r}"
            if stop_reason == "tool_calls":
                # The --max-tool-calls cap, named as claude_code's hook names it.
                reason = f"TOOL_BUDGET_EXHAUSTED: max_tool_calls reached ({reason})"
            self._status = BackendStatus(
                state="error", error_reason=reason, tokens_processed=tokens,
                stop_reason=str(stop_reason) if stop_reason else None,
            )

    def _blocked_status(self, questions: object, tokens: int | None) -> BackendStatus:
        if not isinstance(questions, list) or not questions:
            # A blocked result always carries what the model asked; an empty
            # list is a contract violation, not a state to wait in.
            return BackendStatus(
                state="error",
                error_reason="nare reported blocked with an empty questions list",
                tokens_processed=tokens,
            )
        return BackendStatus(
            state="blocked", questions=[str(q) for q in questions], tokens_processed=tokens,
        )

    def _handle_exit(self, returncode: int) -> None:
        """Exit codes are outcomes: only a run with no result line is decided here."""
        if self._saw_result and returncode == 0:
            return
        if self._status.state != "working":
            return
        tail = " | ".join(list(self._log_buffer)[-10:])
        if returncode == 0:
            # nare writes a result line for every run that started, so a clean
            # exit without one is a crash; waiting on it would hang the card.
            reason = "nare exited without a result line"
            self._status = BackendStatus(
                state="error", error_reason=f"{reason}: {tail}" if tail else reason,
            )
            return
        if returncode == _EXIT_CONFIG_ERROR:
            # Never started: say why, from nare's stderr, and do not retry.
            log.error("nare.config_error", stderr_tail=tail)
            reason = "nare refused to start (exit 2): adapter/configuration error"
            self._status = BackendStatus(
                state="error", error_reason=f"{reason}: {tail}" if tail else reason,
            )
        elif returncode != 0:
            self._status = BackendStatus(
                state="error",
                error_reason=(
                    f"nare exited rc={returncode}: {tail}" if tail
                    else f"nare exited rc={returncode}"
                ),
            )

    async def _terminate(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            pgid = os.getpgid(self._proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            log.debug("nare.killpg_term_skipped")
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except TimeoutError:
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
                            log.debug("nare.kill_child_gone")
                    parent.kill()
                except psutil.NoSuchProcess:
                    log.debug("nare.kill_parent_gone")


def _build_task_prompt(score: Score, *, stand_path: pathlib.Path | None = None) -> str:
    """The task prompt. The persona rides ``--append-system-file``, after nare's
    own system prompt, so nare keeps its scope rules (#314)."""
    parts: list[str] = [f"# Task: {score.title}", ""]
    if score.description:
        parts += [score.description, ""]
    parts += card_docs_prompt_section(score, stand_path)
    parts += qa_findings_prompt_section(score)
    parts += repair_mandate_prompt_section(score)
    parts += scanner_findings_prompt_section(score)
    parts += scope_prompt_section(score, "nare")
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
    parts += ["", "---"]
    parts += role_closing_section(score)
    return "\n".join(parts).strip()


def _feedback_sections(score: Score) -> list[str]:
    """126: disputes of this stage's own prior findings, to adjudicate rather
    than treat as fresh work items. Buffered human feedback rides the next turn."""
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


def _tool_result(detail: object) -> dict[str, Any] | None:
    """The tool result record of a result ``progress`` event, else None."""
    if isinstance(detail, dict):
        record = detail.get("tool_result")
        if isinstance(record, dict):
            return record
    return None


def _detail_text(detail: object) -> str:
    if detail is None or detail == {}:
        return ""
    if isinstance(detail, str):
        return detail
    return json.dumps(detail, ensure_ascii=False, default=str)


def _final_text(result: dict[str, object]) -> str | None:
    """The run's answer: the validated ``output`` document under ``--schema``,
    otherwise ``text``, the final assistant reply."""
    output = result.get("output")
    if output is not None:
        if isinstance(output, str):
            return output.strip() or None
        return json.dumps(output, ensure_ascii=False)
    text = str(result.get("text") or "").strip()
    return text or None


def _usage_tokens(usage: object) -> int | None:
    """``usage.total`` (nare's own sum of the four token counters); otherwise the
    counters that are present. None when nare reported nothing, never a zero."""
    if not isinstance(usage, dict):
        return None
    total = _count(usage.get("total"))
    if total:
        return total
    counted = sum(
        value for value in (
            _count(usage.get(field)) for field in ("input", "output", "cache_read", "cache_write")
        ) if value
    )
    return counted or None
