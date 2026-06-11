"""Compatibility-first performer backend (LCD OpenAI-compatible profile).

Implements a ``/v1/chat/completions`` request shape that is the lowest-common-
denominator of OpenAI, LM Studio, vLLM, Ollama, and LiteLLM proxy — no hosted
tool descriptors (only ``type: function``), no ``developer`` role, no
``prompt_cache_key``, no other OpenAI-only extensions.

Adapter-per-harness: this module mirrors the surface of
``performer.backends.opencode`` but does **not** subclass it
(see ``specs/067-compatibility-first-backend/research.md`` R1/R2 and feedback
memories ``feedback_junie_own_harness`` / ``feedback_interface_first_design``).

Scope of the in-process guards
------------------------------
``start()`` launches ``opencode serve`` as a subprocess and the LCD-shape
contract for **production traffic** is enforced by the env it is started with
(``OPENAI_BASE_URL``, ``OPENAI_API_KEY``, ``OPENCODE_DISABLE_HOSTED_TOOLS=1``).
The in-process helpers in this module — ``_assert_lcd_payload``,
``_remap_developer_role``, ``_redact_request_body``, and
``_post_chat_completions`` — only fire on direct chat-completions calls made
through *this* adapter instance (currently exercised by the contract /
integration tests in ``tests/{unit,integration}/test_067_*``). They do **not**
intercept the requests opencode itself emits from its subprocess. See spec 067
research.md R1/R3 for the rationale.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psutil
import structlog

from performer.backends._card_docs import card_docs_prompt_section
from performer.backends._env_policy import build_subprocess_env
from performer.backends._lcd_helpers import (
    redact_request_body as _redact_request_body,
    strip_base_url_credentials as _strip_base_url_creds_local,
    truncate_body as _truncate_body_local,
)
from performer.backends.base import BackendStatus
from performer.io_utils import iter_lines_chunked
from performer.models import DIAGNOSTIC_ROLE, BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200
_READY_POLL_INTERVAL = 0.2
_READY_TIMEOUT = 30.0
_JSON_ONLY_ROLES = {
    "assessing", "assessor", "reviewing", "reviewer", "closing_review",
    "closer", "security", "qa", "documenting", "tech_writer",
}

# ---------------------------------------------------------------------------
# LCD payload contract (data-model.md §1)
# ---------------------------------------------------------------------------

_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "model", "messages", "tools", "tool_choice",
    "temperature", "max_tokens", "stream", "response_format",
})
_ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})
_FORBIDDEN_TOP_LEVEL = frozenset({"prompt_cache_key", "parallel_tool_calls", "user"})


class LcdPayloadError(ValueError):
    """Raised when an outbound payload violates the LCD whitelist/denylist.

    This is a programming error — the prompt template or tool registry
    constructed a payload incompatible with the LCD profile. Not an operator
    misconfiguration.
    """


def _assert_lcd_payload(body: dict) -> None:
    """Validate ``body`` matches the LCD ``/v1/chat/completions`` shape.

    Raises ``LcdPayloadError`` on the first violation, naming the offending
    field. See ``specs/067-compatibility-first-backend/data-model.md`` §1.
    """
    if not isinstance(body, dict):
        raise LcdPayloadError(f"body must be a dict, got {type(body).__name__}")

    for key in _FORBIDDEN_TOP_LEVEL:
        if key in body:
            raise LcdPayloadError(
                f"forbidden top-level field {key!r}: not portable across LCD endpoints"
            )

    response_format = body.get("response_format")
    if response_format is not None:
        if not isinstance(response_format, dict):
            raise LcdPayloadError("response_format must be an object")
        if response_format.get("type") not in (None, "json_object", "text"):
            raise LcdPayloadError(
                f"response_format.type={response_format.get('type')!r} not portable; "
                "only 'json_object' / 'text' are LCD-safe"
            )
        if "json_schema" in response_format:
            raise LcdPayloadError(
                "response_format.json_schema is an OpenAI-only extension"
            )

    messages = body.get("messages")
    if messages is not None:
        if not isinstance(messages, list):
            raise LcdPayloadError("messages must be a list")
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                raise LcdPayloadError(f"messages[{idx}] must be a dict")
            if "role" not in msg:
                raise LcdPayloadError(f"messages[{idx}] missing required 'role' field")
            role = msg["role"]
            if role == "developer":
                raise LcdPayloadError(
                    f"messages[{idx}].role='developer' is not portable; "
                    "use _remap_developer_role() before validation, or re-enable "
                    "the automatic remap (OPENCODE_COMPAT_REMAP_DEVELOPER_ROLE=1 / "
                    "compat_remap_developer_role=True)"
                )
            if role not in _ALLOWED_ROLES:
                raise LcdPayloadError(
                    f"messages[{idx}].role={role!r} not in LCD allowed roles {sorted(_ALLOWED_ROLES)}"
                )

    tools = body.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise LcdPayloadError("tools must be a list")
        for idx, tool in enumerate(tools):
            if not isinstance(tool, dict):
                raise LcdPayloadError(f"tools[{idx}] must be a dict")
            tool_type = tool.get("type")
            if tool_type != "function":
                raise LcdPayloadError(
                    f"tools[{idx}].type={tool_type!r} is a hosted-tool descriptor; "
                    "only type='function' is LCD-portable"
                )


def _remap_developer_role(
    messages: list[dict],
    *,
    warned_flag: dict[str, bool] | None = None,
    logger: Any = None,
) -> list[dict]:
    """Convert ``role: developer`` → ``role: system`` per research.md R4.

    Emits a one-time WARN (deduped via ``warned_flag``) the first time a
    ``developer`` role is seen. ``warned_flag`` is a mutable dict whose
    ``'warned'`` key is flipped to True after the first emission — pass the
    same instance dict across the adapter session to dedupe.
    """
    if not isinstance(messages, list):
        return messages
    remapped: list[dict] = []
    any_remapped = False
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "developer":
            new_msg = dict(msg)
            new_msg["role"] = "system"
            remapped.append(new_msg)
            any_remapped = True
        else:
            remapped.append(msg)
    if any_remapped and warned_flag is not None and not warned_flag.get("warned"):
        emitter = logger or log
        emitter.warning(
            "opencode_compat.developer_role_remapped",
            note="role='developer' remapped to 'system' (LCD profile); "
                 "set OPENCODE_COMPAT_REMAP_DEVELOPER_ROLE=0 (or pass "
                 "compat_remap_developer_role=False) to disable",
            spec="067",
        )
        warned_flag["warned"] = True
    return remapped


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OpenCodeCompatAdapter:
    """Backend adapter for the LCD OpenAI-compatible profile.

    Lifecycle mirrors ``OpenCodeAdapter`` (start / get_status / drain_events /
    relay_feedback / stop) but does **not** subclass — copy-and-adjust per
    feedback_junie_own_harness. Launches ``opencode serve`` with environment
    that disables hosted-tool descriptors and pins the upstream base_url.

    Direct ``/v1/chat/completions`` calls (when any code path constructs one)
    flow through ``_post_chat_completions`` which:
      1. Runs ``_remap_developer_role`` (gated on ``compat_remap_developer_role``)
      2. Asserts the LCD shape via ``_assert_lcd_payload``
      3. Emits a redacted debug log
      4. POSTs via httpx
      5. On non-2xx, attaches an ``upstream_http_error`` envelope to the
         caller's metrics and returns the response unchanged.
    """

    def __init__(
        self,
        *,
        executable: str = "opencode",
        adapter_name: str = "opencode_compat",
        base_url: str | None = None,
        api_key: str | None = None,
        compat_remap_developer_role: bool | None = None,
    ) -> None:
        self._executable = executable
        self._adapter_name = adapter_name
        # Normalise base_url so URL construction is unambiguous (RFC 3986 join
        # would otherwise drop the base_url path when an absolute "/chat/..."
        # is posted; see _post_chat_completions).
        self._base_url = base_url.rstrip("/") if base_url else base_url
        self._api_key = api_key
        # Env override for the developer→system remap, so operators can flip it
        # without re-deploying. Kwarg wins if explicitly passed.
        if compat_remap_developer_role is None:
            env_val = os.environ.get("OPENCODE_COMPAT_REMAP_DEVELOPER_ROLE")
            if env_val is None:
                compat_remap_developer_role = True
            else:
                compat_remap_developer_role = env_val.strip().lower() not in {
                    "0", "false", "no", "off", "",
                }
        self._compat_remap_developer_role = compat_remap_developer_role
        self._warned_developer = {"warned": False}

        self._proc: asyncio.subprocess.Process | None = None
        self._port: int | None = None
        self._session_id: str | None = None
        self._workspace_dir: str | None = None
        self._status: BackendStatus = BackendStatus(state="working")
        self._reader_task: asyncio.Task[None] | None = None
        self._log_drain_task: asyncio.Task[None] | None = None
        self._event_buffer: deque[BackendEvent] = deque(maxlen=200)
        self._log_buffer: deque[str] = deque(maxlen=200)
        self._client: httpx.AsyncClient | None = None
        # Last upstream HTTP error seen by _post_chat_completions, surfaced via
        # PerformerResponse.metrics.upstream_http_error by the orchestration layer.
        self.last_upstream_http_error: dict | None = None

    @property
    def base_url(self) -> str | None:
        """Configured upstream base URL (already trailing-slash-normalised)."""
        return self._base_url

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
        port = _find_free_port()
        self._port = port
        self._workspace_dir = str(stand.path)

        # LCD-flavoured env: pin base_url + key for opencode, disable any
        # hosted-tool descriptors the CLI might otherwise advertise.
        compat_env: dict[str, str] = {}
        if self._base_url:
            compat_env["OPENAI_BASE_URL"] = self._base_url
            compat_env["OPENAI_API_BASE"] = self._base_url
        if self._api_key:
            compat_env["OPENAI_API_KEY"] = self._api_key
        compat_env["OPENCODE_DISABLE_HOSTED_TOOLS"] = "1"

        # 088 B1 shared env policy: the CLI must launch on the IMAGE's node
        # (the env-cache pins the project's .nvmrc node, e.g. 18.12.1, which
        # crashes modern Node CLIs at startup). Image PATH first, cache
        # toolchain dirs appended; every other cache var flows through.
        env = build_subprocess_env(
            cache_env=stand.cache_env,
            git_env=stand.git_env,
            tool_env=score.tool_env,
            extra=compat_env,
        )

        self._proc = await asyncio.create_subprocess_exec(
            self._executable, "serve",
            "--port", str(port),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(stand.path),
            start_new_session=True,
            env=env,
        )
        self._log_drain_task = asyncio.create_task(
            self._drain_logs(), name=f"{self._adapter_name}-log-drain"
        )
        await self._wait_for_ready(port)

        self._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"x-opencode-directory": str(stand.path)},
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0),
        )

        session_body: dict = {}
        if model:
            session_body["modelID"] = model
        if max_tokens is not None:
            session_body["maxTokens"] = max_tokens
        resp = await self._client.post("/session", json=session_body if session_body else None)
        resp.raise_for_status()
        self._session_id = resp.json()["id"]
        log.info(
            "backend.session_created",
            backend=self._adapter_name,
            session_id=self._session_id,
            port=port,
        )

        task_text = _build_task_prompt(score, stand_path=Path(stand.path))
        await self._client.post(
            f"/session/{self._session_id}/prompt_async",
            json={"parts": [{"type": "text", "text": task_text}]},
        )
        log.info(
            "backend.task_dispatched",
            backend=self._adapter_name,
            session_id=self._session_id,
        )

        self._reader_task = asyncio.create_task(
            self._event_reader_loop(), name=f"{self._adapter_name}-event-reader"
        )

    def get_status(self) -> BackendStatus:
        return self._status

    def drain_events(self) -> list[BackendEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events

    async def relay_feedback(self, feedback: str) -> None:
        if self._client is None or self._session_id is None:
            log.warning("relay_feedback called but session not active")
            return
        await self._client.post(
            f"/session/{self._session_id}/prompt_async",
            json={"parts": [{"type": "text", "text": feedback}]},
        )
        self._status = BackendStatus(state="working")
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(
                self._event_reader_loop(), name=f"{self._adapter_name}-event-reader"
            )

    async def stop(self) -> None:
        for task in (self._reader_task, self._log_drain_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        if self._client is not None:
            await self._client.aclose()
            self._client = None

        if self._proc is not None and self._proc.returncode is None:
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
                            pass
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        log.info("backend.serve_stopped", backend=self._adapter_name)

    # ------------------------------------------------------------------
    # Direct chat-completions path (T013/T014)
    # ------------------------------------------------------------------

    async def _post_chat_completions(
        self,
        body: dict,
        *,
        client: httpx.AsyncClient | None = None,
        route: str = "POST /v1/chat/completions",
    ) -> httpx.Response:
        """Send an LCD-validated chat-completions request.

        Caller passes the request ``body`` dict; this method runs the
        developer-role remap (if enabled), validates against the LCD whitelist,
        emits a redacted debug log, then POSTs. On non-2xx, sets
        ``self.last_upstream_http_error`` (envelope dict per
        ``specs/067-compatibility-first-backend/contracts/upstream_http_error.md``)
        and returns the response — caller is responsible for surfacing it via
        ``PerformerResponse.metrics.upstream_http_error``.

        Pass ``client`` to reuse a pooled ``httpx.AsyncClient`` (production
        path) or to inject a ``httpx.MockTransport`` (test harness). When
        omitted, a fresh client is created and closed per call.
        """
        if self._compat_remap_developer_role and isinstance(body.get("messages"), list):
            body = {
                **body,
                "messages": _remap_developer_role(
                    body["messages"], warned_flag=self._warned_developer, logger=log
                ),
            }
        # Raises BEFORE any network call — surfaced as LcdPayloadError to the
        # caller, not as an upstream_http_error envelope (it's a programming
        # error in the prompt/tool registry, not an operator misconfig).
        try:
            _assert_lcd_payload(body)
        except LcdPayloadError as exc:
            log.warning(
                "opencode_compat.lcd_payload_rejected",
                route=route,
                error=str(exc),
            )
            raise

        log.debug(
            "opencode_compat.outbound_request",
            route=route,
            body=_redact_request_body(body),
        )

        # Build the absolute request URL ourselves so the base_url path (e.g.
        # ``/v1``) is preserved. httpx's default behaviour follows RFC 3986
        # resolution — a leading-slash relative URL replaces the base_url's
        # path, which would silently drop ``/v1`` for LM Studio / vLLM and
        # send the request to ``http://host/chat/completions``.
        if self._base_url:
            request_url = f"{self._base_url}/chat/completions"
        else:
            route_parts = route.split(" ", 1)
            if len(route_parts) != 2 or not route_parts[1].startswith("/"):
                raise ValueError(
                    f"route must be 'METHOD /path' when base_url is unset, got {route!r}"
                )
            request_url = route_parts[1]

        post_client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0),
        )
        owns_client = client is None
        started = time.monotonic()
        try:
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
            response = await post_client.post(
                request_url,
                json=body,
                headers=headers,
            )
        finally:
            if owns_client:
                await post_client.aclose()
        elapsed_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 300:
            raw_body = response.text
            truncated_body, was_truncated = _truncate_body_local(raw_body)
            upstream_request_id = (
                response.headers.get("x-request-id")
                or response.headers.get("openai-request-id")
            )
            self.last_upstream_http_error = {
                "kind": "upstream_http_error",
                "status": response.status_code,
                "body": truncated_body,
                "body_truncated": was_truncated,
                "route": route,
                "base_url": _strip_base_url_creds_local(self._base_url or ""),
                "upstream_request_id": upstream_request_id,
                "elapsed_ms": elapsed_ms,
                "occurred_at": datetime.now(UTC).isoformat(),
            }
        else:
            # Clear any stale envelope from a previous failed call so callers
            # never read a success/error mismatch off this adapter instance.
            self.last_upstream_http_error = None
        return response

    # ------------------------------------------------------------------
    # Internal helpers (mirror OpenCodeAdapter)
    # ------------------------------------------------------------------

    async def _wait_for_ready(self, port: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _READY_TIMEOUT
        async with httpx.AsyncClient(timeout=1.0) as probe:
            while loop.time() < deadline:
                try:
                    resp = await probe.get(f"http://127.0.0.1:{port}/global/health")
                    if resp.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(_READY_POLL_INTERVAL)
        raise RuntimeError(
            f"{self._adapter_name} serve did not become ready within {_READY_TIMEOUT}s"
        )

    async def _drain_logs(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            async for raw in iter_lines_chunked(self._proc.stdout):
                line = raw.decode(errors="replace").rstrip()
                if line:
                    self._log_buffer.append(line)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.debug("backend.log_drain_error", backend=self._adapter_name, error=str(exc))

    async def _event_reader_loop(self) -> None:
        if self._client is None:
            return
        try:
            async with self._client.stream("GET", "/event") as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        event = json.loads(data_str)
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
            if self._status.state == "working":
                await self._resolve_final_status()

    async def _resolve_final_status(self) -> None:
        if self._client is None or self._session_id is None:
            self._status = BackendStatus(state="done")
            return
        try:
            resp = await self._client.get(f"/session/{self._session_id}")
            if resp.status_code == 200:
                info = resp.json()
                if info.get("time", {}).get("idle"):
                    self._status = BackendStatus(state="done")
                    return
        except Exception:
            pass
        self._status = BackendStatus(state="done")

    def _emit(self, type: BackendEventType, text: str, detail: str = "") -> None:
        self._event_buffer.append(BackendEvent(type=type, text=text[:_MAX_TEXT], detail=detail))

    def _handle_event(self, event: dict) -> None:
        payload = event.get("payload", event)
        event_type = payload.get("type", "")
        props = payload.get("properties", {})

        if event_type == "session.updated":
            session = props.get("session", props)
            time_info = session.get("time", {})
            if time_info.get("idle"):
                self._status = BackendStatus(state="done")
            elif session.get("error") or time_info.get("error"):
                reason = str(session.get("error", "unknown error"))
                self._status = BackendStatus(state="error", error_reason=reason)
        elif event_type == "session.idle":
            self._status = BackendStatus(state="done")
        elif event_type == "session.error":
            reason = props.get("error", {}).get("message", str(props))
            self._status = BackendStatus(state="error", error_reason=reason)
            self._emit(BackendEventType.error, reason)
        elif event_type == "message.part.updated":
            part = props.get("part", {})
            part_type = part.get("type", "")
            if part_type == "text":
                text = part.get("text", "")[:_MAX_TEXT]
                if text:
                    self._status = BackendStatus(state="working", progress=text)
                    self._emit(BackendEventType.progress, text)
            elif part_type in ("tool-input", "tool_input"):
                tool = part.get("toolName", part.get("tool_name", "tool"))
                input_text = str(part.get("input", ""))[:_MAX_TEXT]
                self._emit(BackendEventType.tool_use, f"{tool}: {input_text}", detail=tool)
            elif part_type in ("tool-output", "tool_output"):
                tool = part.get("toolName", part.get("tool_name", "tool"))
                output_text = str(part.get("output", ""))[:_MAX_TEXT]
                self._emit(BackendEventType.tool_use, f"{tool} → {output_text}", detail=tool)
        elif event_type in ("session.message", "message.updated"):
            parts = props.get("parts", [])
            questions = [
                p.get("text", "")
                for p in parts
                if p.get("type") in ("question", "needsInput") and p.get("text")
            ]
            if questions:
                self._status = BackendStatus(state="blocked", questions=questions)


def _build_task_prompt(
    score: Score, *, stand_path: Path | None = None
) -> str:
    """Build the initial task prompt — identical surface to opencode._build_task_prompt."""
    parts: list[str] = []
    if score.persona_instructions:
        parts += ["## Role Instructions", "", score.persona_instructions, ""]
    parts += [f"# Task: {score.title}", ""]
    if score.description:
        parts += [score.description, ""]
    parts += card_docs_prompt_section(score, stand_path)
    if score.acceptance_criteria:
        parts += ["## Acceptance Criteria", ""]
        parts.extend(f"- {c}" for c in score.acceptance_criteria)
    if score.clarifications:
        parts += ["", "## Clarification Q&A", ""]
        for entry in score.clarifications:
            questions = entry.get("questions") or []
            answer = str(entry.get("answer", "")).strip()
            if questions:
                parts.append("**Questions asked:**")
                parts.extend(f"- {q}" for q in questions)
            if answer:
                parts += [f"**Answer:** {answer}", ""]
    if score.pr_diff:
        parts += [
            "", "## PR Diff Under Review", "",
            "The unified diff below is the complete set of changes on this PR. "
            "Review it directly — do NOT report that no changes were supplied.",
            "", "```diff", score.pr_diff.rstrip("\n"), "```", "",
        ]
    if score.relay_feedback:
        parts += [
            "", "## Human Feedback (address ALL of these issues)", "",
            "IMPORTANT: These comments may only tag a few examples. Search the entire "
            "codebase for ALL similar occurrences of the same pattern and fix them all.",
            "",
        ]
        for item in score.relay_feedback:
            if isinstance(item, dict):
                body = item.get("body", "")
                if body:
                    parts.append(f"- {body}")
                inline = item.get("comments", [])
                if isinstance(inline, list):
                    for c in inline:
                        if isinstance(c, dict):
                            c_body = c.get("body", "")
                            c_path = c.get("path", "")
                            c_line = c.get("line")
                            if c_body:
                                loc = (
                                    f"`{c_path}:{c_line}`" if c_path and c_line
                                    else (f"`{c_path}`" if c_path else "")
                                )
                                parts.append(f"  - {loc} — {c_body}" if loc else f"  - {c_body}")
            elif isinstance(item, str):
                parts.append(f"- {item}")
    parts += ["", "---"]
    if score.role == DIAGNOSTIC_ROLE:
        parts += [
            "This is a one-off diagnostic/benchmark task. Use any tools at your "
            "disposal to complete it. You do NOT need to commit, push, or open a "
            "pull request — just perform the task and report what you did.",
        ]
    elif score.role in _JSON_ONLY_ROLES:
        parts += [
            "Return ONLY a valid JSON object for your role contract.",
            "Do not include markdown, prose, or code fences.",
        ]
    else:
        parts += [
            "Complete the task above. Commit your changes with a clear, descriptive commit message.",
            "Do not push or open a pull request — this will be handled automatically after you finish.",
        ]
    return "\n".join(parts)
