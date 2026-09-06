"""JunieBackend — drives JetBrains Junie as a one-shot CLI agent.

Junie is its own complete agent harness (plan → edit → commit, autonomous),
so this adapter mirrors the ``claude_code`` pattern rather than the OpenCode
``serve`` HTTP protocol:

    start()          → launches ``junie --task <prompt> --project <path>
                       --output-format json --json-output-file <tmp>``,
                       waits for exit; final status comes from the JSON file
    get_status()     → returns current BackendStatus (non-blocking)
    drain_events()   → return and clear buffered BackendEvent list
    relay_feedback() → re-invokes junie with the feedback prepended to the
                       original task (Junie has no documented ``--resume``)
    stop()           → kills process group; waits for subprocess to exit

Custom-LLM support
------------------
When ``JUNIE_PROVIDER_BASE_URL`` is set in the environment, the adapter
writes a profile JSON to ``$JUNIE_HOME/models/<profile_id>.json`` (``$JUNIE_HOME``
defaults to ``~/.junie``; relocated to a writable temp dir if the configured
home is read-only) and selects it via ``--model custom:<profile_id>``.

Two Junie-specific contract details (verified empirically — see the inline
comments in ``_maybe_write_custom_profile``):
  - the profile's ``id`` FIELD is the upstream/wire model name (e.g.
    ``local/qwen3.6:35b``, from ``JUNIE_PROVIDER_MODEL``), NOT the local
    profile id (which is the filename / ``--model custom:`` selector);
  - ``JUNIE_PROVIDER_BASE_URL`` must be the FULL endpoint URL
    (``https://host/v1/chat/completions``), not a ``/v1`` base — Junie POSTs to
    it verbatim. This differs from codex/pi/opencode, which take a ``/v1`` base.
See https://junie.jetbrains.com/docs/custom-llm-models.html.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
from collections import deque
from pathlib import Path

import psutil
import structlog

from performer.backends._card_docs import card_docs_prompt_section, qa_findings_prompt_section, brief_prompt_sections
from performer.backends._env_policy import build_subprocess_env
from performer.backends.base import BackendStatus
from performer.models import DIAGNOSTIC_ROLE, LOCAL_CAPTURE_RULE, BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200
# Profile id flows into a file path (`$JUNIE_HOME/models/<id>.json`) and into
# the `--model custom:<id>` CLI arg, so reject anything that could escape the
# models directory or smuggle shell metacharacters.  Mirrors the bare-key rule
# used for CODEX_PROVIDER_NAME in performer.backends.codex.
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_JSON_ONLY_ROLES = {
    "assessing",
    "assessor",
    "reviewing",
    "reviewer",
    "closing_review",
    "closer",
    "security",
    "qa",
    "documenting",
    "tech_writer",
}


def _maybe_write_custom_profile() -> str | None:
    """Write a Junie custom-LLM profile JSON from JUNIE_PROVIDER_* env vars.

    Returns the profile id (for ``--model custom:<id>``) when a base URL is
    configured, or ``None`` to fall back to Junie's default model selection.
    """
    base_url = os.environ.get("JUNIE_PROVIDER_BASE_URL", "").strip()
    if not base_url:
        return None
    # The *local* profile id = the filename (`<profile_id>.json`) and the
    # `--model custom:<profile_id>` selector; it must be path/CLI-safe.
    profile_id = os.environ.get("JUNIE_PROVIDER_MODEL_ID", "vivi").strip() or "vivi"
    if not _PROFILE_ID_RE.match(profile_id):
        raise ValueError(
            f"unsafe JUNIE_PROVIDER_MODEL_ID (must match [A-Za-z0-9_-]+): {profile_id!r}"
        )
    api_type = os.environ.get("JUNIE_PROVIDER_API_TYPE", "OpenAICompletion").strip()
    # Per Junie's custom-LLM schema, the profile's `id` FIELD is "the model
    # identifier as expected by the API endpoint" — i.e. the upstream/wire model
    # name (e.g. local/qwen3.6:35b), NOT the local profile id. Junie sends this
    # verbatim as the request `model`. Use the configured wire model; fall back
    # to the profile id only if unset. (Verified: setting id=<profile_id> made
    # LiteLLM 400 with "no healthy deployments for model=vivi".)
    wire_model = os.environ.get("JUNIE_PROVIDER_MODEL", "").strip() or profile_id
    # `baseUrl` must be the FULL endpoint URL — Junie's OpenAICompletion client
    # POSTs to baseUrl verbatim (no path appended), so it must be e.g.
    # https://host/v1/chat/completions, NOT a https://host/v1 base. (Verified:
    # a /v1 base → POST /v1 → 404; host root → POST / → 405.)
    profile = {
        "id": wire_model,
        "baseUrl": base_url,
        "apiType": api_type,
    }
    if api_key_env := os.environ.get("JUNIE_PROVIDER_API_KEY_ENV"):
        if value := os.environ.get(api_key_env):
            profile["apiKey"] = value

    junie_home = Path(os.environ.get("JUNIE_HOME") or Path.home() / ".junie")
    try:
        _write_profile(junie_home, profile_id, profile)
    except OSError:
        # JUNIE_HOME is read-only — e.g. the operator mounts ~/.junie creds
        # read-only (the recommended secure default), but Junie writes custom-LLM
        # profiles under $JUNIE_HOME/models/. Fall back to a writable job-scoped
        # home seeded from the read-only creds, and export JUNIE_HOME so the
        # junie subprocess (which inherits os.environ) finds both the creds and
        # the freshly-written profile. Mirrors hermes' job-scoped HERMES_HOME.
        writable_home = Path(tempfile.mkdtemp(prefix="junie-home-"))
        _seed_junie_home(junie_home, writable_home)
        os.environ["JUNIE_HOME"] = str(writable_home)
        _write_profile(writable_home, profile_id, profile)
        log.info(
            "junie.custom_profile_writable_home",
            source=str(junie_home),
            home=str(writable_home),
            reason="source JUNIE_HOME read-only",
        )
    return profile_id


def _write_profile(home: Path, profile_id: str, profile: dict) -> None:
    """Write ``<home>/models/<profile_id>.json``. Raises OSError if read-only."""
    models_dir = home / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / f"{profile_id}.json").write_text(json.dumps(profile, indent=2))


def _seed_junie_home(src: Path, dst: Path) -> None:
    """Best-effort copy of an existing (read-only) JUNIE_HOME into a writable one.

    Carries over Junie's auth/license files so the subprocess stays authenticated
    when we relocate JUNIE_HOME. Skips the ``models`` dir (we rewrite it) and
    swallows per-entry copy errors so a partial creds dir can't block dispatch.
    """
    if not src.exists():
        return
    try:
        entries = list(src.iterdir())
    except OSError:
        return
    for item in entries:
        if item.name == "models":
            continue
        target = dst / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        except OSError:
            continue


class JunieBackend:
    """Backend adapter that drives the ``junie`` CLI in headless mode."""

    def __init__(self) -> None:
        self._executable = os.environ.get("JUNIE_EXECUTABLE", "junie")
        self._proc: asyncio.subprocess.Process | None = None
        self._status: BackendStatus = BackendStatus(state="working")
        self._reader_task: asyncio.Task[None] | None = None
        self._event_buffer: deque[BackendEvent] = deque(maxlen=200)
        self._stand: Stand | None = None
        self._score: Score | None = None
        self._git_env: dict[str, str] = {}
        self._cache_env: dict[str, str] = {}
        self._tool_env: dict[str, str] = {}
        self._model: str | None = None
        self._max_tokens: int | None = None
        self._json_output_path: Path | None = None
        self._original_prompt: str = ""

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
        self._stand = stand
        self._score = score
        self._git_env = stand.git_env
        self._cache_env = stand.cache_env
        self._tool_env = score.tool_env
        self._model = model
        self._max_tokens = max_tokens
        self._original_prompt = _build_task_prompt(score, stand_path=Path(stand.path))
        await self._launch(self._original_prompt)

    def get_status(self) -> BackendStatus:
        return self._status

    def drain_events(self) -> list[BackendEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events

    async def relay_feedback(self, feedback: str) -> None:
        """Re-invoke junie with the feedback prepended to the original task.

        Junie has no documented ``--resume`` flag, so context is preserved by
        re-running on the same workspace (git history holds prior edits) with
        the feedback hoisted above the original prompt.
        """
        await self.stop()
        if self._stand is None:
            return
        combined = (
            "## Human Feedback (address ALL of these issues)\n\n"
            f"{feedback}\n\n"
            "---\n\n"
            f"{self._original_prompt}"
        )
        # Reset status BEFORE _launch — _launch schedules _wait_and_parse as a
        # background task, and on a fast-failing subprocess that task can set
        # state="error" before we return.  Setting "working" after _launch
        # would clobber that real terminal state.
        self._status = BackendStatus(state="working")
        await self._launch(combined)

    async def stop(self) -> None:
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
        self._cleanup_json_output()
        log.info("junie stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _launch(self, prompt: str) -> None:
        """Start a junie subprocess with the given prompt."""
        if self._stand is None:
            raise RuntimeError("JunieBackend._launch called before start()")

        custom_profile_id = _maybe_write_custom_profile()
        effective_model = (
            f"custom:{custom_profile_id}" if custom_profile_id else self._model
        )

        # JSON output file: junie writes the final result here on exit.
        json_fd, json_path = tempfile.mkstemp(prefix="junie-out-", suffix=".json")
        os.close(json_fd)
        self._json_output_path = Path(json_path)

        args = [
            self._executable,
            "--project", str(self._stand.path),
            "--output-format", "json",
            "--json-output-file", str(self._json_output_path),
            "--skip-update-check",
            "--input-format", "text",
        ]
        if effective_model:
            args += ["--model", effective_model]
        # Pass the task last as positional argument (per docs).
        args += ["--task", prompt]

        # 088 B1 shared env policy: junie is a Node CLI — it must launch on
        # the IMAGE's node, never the env-cache's project-pinned node (e.g.
        # 18.12.1 → startup crash). Image PATH first, cache toolchain dirs
        # appended; every other cache var flows through.
        env = build_subprocess_env(
            cache_env=self._cache_env,
            git_env=self._git_env,
            tool_env=self._tool_env,
        )
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._stand.path),
                start_new_session=True,
                env=env,
            )
        except Exception:
            self._cleanup_json_output()
            raise
        self._reader_task = asyncio.create_task(
            self._wait_and_parse(), name="junie-reader"
        )
        log.info("junie started", pid=self._proc.pid, model=effective_model)

    def _cleanup_json_output(self) -> None:
        """Remove the temp JSON output file if it still exists."""
        if self._json_output_path and self._json_output_path.exists():
            try:
                self._json_output_path.unlink()
            except OSError:
                pass
        self._json_output_path = None

    def _emit(self, type: BackendEventType, text: str, detail: str = "") -> None:
        self._event_buffer.append(BackendEvent(type=type, text=text[:_MAX_TEXT], detail=detail))

    async def _wait_and_parse(self) -> None:
        """Wait for junie to exit, then derive final status from JSON output."""
        if self._proc is None:
            return
        try:
            stdout_b, stderr_b = await self._proc.communicate()
            rc = self._proc.returncode
            stdout_text = stdout_b.decode(errors="replace") if stdout_b else ""
            stderr_text = stderr_b.decode(errors="replace") if stderr_b else ""

            parsed: dict | None = None
            if self._json_output_path and self._json_output_path.exists():
                try:
                    parsed = json.loads(self._json_output_path.read_text())
                except json.JSONDecodeError:
                    parsed = None
                finally:
                    # Clear the path so a subsequent stop()/_cleanup_json_output
                    # doesn't re-attempt unlink on a stale reference.
                    try:
                        self._json_output_path.unlink()
                    except OSError:
                        pass
                    self._json_output_path = None

            if isinstance(parsed, dict):
                self._apply_parsed_result(parsed, rc, stderr_text)
            else:
                if rc == 0:
                    self._status = BackendStatus(state="done")
                else:
                    reason = stderr_text.strip() or stdout_text.strip() or f"junie exited with code {rc}"
                    self._status = BackendStatus(
                        state="error",
                        error_reason=reason[-_MAX_TEXT:],
                    )
                    self._emit(BackendEventType.error, reason[-_MAX_TEXT:])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("junie reader error", error=str(exc))
            self._status = BackendStatus(state="error", error_reason=str(exc))

    def _apply_parsed_result(self, parsed: dict, rc: int | None, stderr_text: str) -> None:
        """Map a Junie JSON result document onto BackendStatus + events."""
        # Best-effort field discovery — Junie's exact schema isn't fully
        # public, so we accept a few common shapes.
        err = parsed.get("error") or parsed.get("error_message")
        usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
        input_t = parsed.get("input_tokens") or usage.get("input_tokens") or 0
        output_t = parsed.get("output_tokens") or usage.get("output_tokens") or 0
        tokens = (input_t or 0) + (output_t or 0)
        cost = parsed.get("cost_usd") or parsed.get("cost")
        if tokens or cost is not None:
            cost_str = f" · ${cost:.4f}" if isinstance(cost, (int, float)) else ""
            self._emit(BackendEventType.cost, f"{tokens:,} tokens{cost_str}")

        if err or (rc not in (None, 0)):
            reason = str(err) if err else (stderr_text.strip() or f"junie exited with code {rc}")
            self._status = BackendStatus(
                state="error",
                error_reason=reason[-_MAX_TEXT:],
                tokens_processed=tokens or None,
            )
            self._emit(BackendEventType.error, reason[-_MAX_TEXT:])
            return

        summary = parsed.get("summary") or parsed.get("result") or parsed.get("message")
        if isinstance(summary, str) and summary:
            self._emit(BackendEventType.progress, summary[:_MAX_TEXT])

        self._status = BackendStatus(
            state="done",
            tokens_processed=tokens or None,
            output=summary if isinstance(summary, str) else None,
        )


def _build_task_prompt(
    score: Score, *, stand_path: Path | None = None
) -> str:
    """Construct the task description sent to Junie as the initial prompt."""
    parts = []

    if score.persona_instructions:
        parts += ["## Role Instructions", "", score.persona_instructions, ""]

    parts += [f"# Task: {score.title}", ""]
    if score.description:
        parts += [score.description, ""]
    parts += card_docs_prompt_section(score, stand_path)
    parts += qa_findings_prompt_section(score)
    parts += brief_prompt_sections(score)
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

    # 126: disputes of this stage's own prior findings — adjudicate, don't
    # treat as fresh work items.
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
                    f"{_d.get('reason', '')}"
                )

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
                                loc = f"`{c_path}:{c_line}`" if c_path and c_line else (f"`{c_path}`" if c_path else "")
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
        if score.role == "qa":
            parts += [
                "QA contract reminder: include a non-empty `verification_steps` array.",
                "For bug fixes, label steps as verification (not reproduction) unless explicitly asked.",
                "Set `visual_validation_required=true` for UI/UX/visual changes and capture at least one artifact in `visual_evidence` for those tasks.",
                "Include `visual_evidence` entries when screenshots/GIFs/videos/artifacts are available.",
                "Include exact capture attempts in `visual_capture_commands` (commands/scripts you ran).",
                LOCAL_CAPTURE_RULE,
                "If visual evidence cannot be captured, include `demo_setup_steps` and `visual_capture_blockers` with concrete details.",
                (
                    "Screenshot uploads: after capturing a screenshot to disk, run "
                    "`performer-upload-screenshot <path>` (available on $PATH). It "
                    "uploads the file to GitHub's user-attachments CDN and prints "
                    "an https://github.com/user-attachments/assets/... URL on stdout. "
                    "Use that URL as `path_or_url` in your `visual_evidence` entry so "
                    "the image renders inline in the PR comment. If the upload command "
                    "is unavailable or fails, keep the local path — the performer "
                    "wrapper will retry the upload before posting."
                ),
            ]
    else:
        parts += [
            "Complete the task above. Commit your changes with a clear, descriptive commit message.",
            "Do not push or open a pull request — this will be handled automatically after you finish.",
        ]
    return "\n".join(parts)
