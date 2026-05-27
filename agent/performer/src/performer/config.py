"""Performer configuration loaded from environment variables."""
from __future__ import annotations

import functools

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-variable configuration for the performer."""

    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    AGENT_BACKEND: str = "opencode"
    # Supported values: "opencode" | "junie" | "cursor" | "claude_code" | "codex"
    AGENT_TIMEOUT: int = 7200  # seconds — 120 minutes

    # Hard ceiling on a single _run_service_inference() invocation (env_bootstrap
    # role).  Without this, a wedged upstream LiteLLM proxy can hang the
    # awaited LLM call forever — JobRunner stays in state="running" and the
    # coordinare's serialize_env_bootstrap gate never releases.  600 s matches
    # the worst-case observed bootstrap inference duration plus headroom.
    SERVICE_INFERENCE_TIMEOUT: int = 600

    # Per-LLM-step timeout inside ``_run_service_inference``.  The
    # SERVICE_INFERENCE_TIMEOUT above is the whole-run ceiling; this gate is
    # tighter and bounds a single ``client.step()`` call so a wedged upstream
    # proxy / model server surfaces in seconds rather than blocking until the
    # 600 s ceiling.  A timeout at this level raises StepTimeoutExceeded which
    # the orchestrator's retry budget catches, so a transient stall consumes
    # one attempt instead of the entire bootstrap.
    SERVICE_INFERENCE_STEP_TIMEOUT: int = 180

    # 036 — GitHub Enterprise: configurable REST API base URL
    GITHUB_API_URL: str = "https://api.github.com"
    CHECK_MAX_ATTEMPTS: int = 8  # max CI fix cycles before blocking the card
    # 065 Fix 14: bail after this many consecutive identical-failure attempts;
    # if the model can't fix it in 2 tries, more grinding won't help.
    CHECK_NO_PROGRESS_LIMIT: int = 2
    # 071 — CI log inlining.  Below this many chars of `output.text`, the
    # performer auto-fetches the workflow log and appends the tail to the
    # backend relay so the model never relies on a separate tool call to see
    # the failure.  Set MAX to 0 to disable inlining entirely.
    CI_LOG_INLINE_MIN_OUTPUT_CHARS: int = 200
    CI_LOG_INLINE_MAX_CHARS: int = 6000

    # 020 — Architect performer settings
    PLAN_FILE_PATH: str = "docs/coordinare-architecture.md"

    # 021 — Reviewer performer settings
    REVIEWER_MAX_CYCLES: int = 3  # max review cycles before blocking for human

    # 022 — Security performer settings
    SECURITY_MAX_CYCLES: int = 3  # max security fix cycles before blocking for human

    # 023 — QA performer settings
    QA_MAX_CYCLES: int = 3  # max QA fix cycles before blocking for human

    # 045 — Backend output parse retry budget
    # When the backend (assessor, reviewer, security, QA, docs) returns output
    # that isn't parseable as a JSON object, retry the backend run up to this
    # many times before bubbling the error to the coordinare.  Each retry is a
    # fresh LLM call (tokens, minutes), so keep this low.  0 disables retry.
    BACKEND_PARSE_RETRIES: int = 1

    # 073 — LiteLLM proxy routing for the claude_code backend.  When
    # LITELLM_PROXY_BASE_URL is non-empty AND the active backend is
    # claude_code, the performer injects these into the claude CLI subprocess
    # as ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN.  Empty defaults preserve
    # the pre-feature byte-identical env baseline (FR-003 / SC-002).
    LITELLM_PROXY_BASE_URL: str = ""
    LITELLM_PROXY_AUTH_TOKEN: str = ""
    # Opt-in raw capture of upstream /v1/messages responses (and matching
    # request bodies) so operators can diagnose CLI parse failures driven by
    # malformed model output.  When set, the shim writes one file per request
    # to this directory.  OFF by default — bodies on disk can contain secrets
    # the model received in its prompt; only enable in operator-controlled
    # debug sessions.  Bytes are NEVER emitted to structured logs (FR-011).
    LITELLM_PROXY_CAPTURE_DIR: str = ""

    # 073 — Idle/no-progress timeout for the claude_code reader loop.
    # ``_event_reader_loop`` reads stream-json lines from the CLI's stdout; if
    # no new line arrives for this many seconds we force a terminal
    # BackendStatus rather than blocking forever.  Catches the case where the
    # CLI receives ``stop_reason: end_turn`` from the upstream but never emits
    # its own terminal ``result.success`` line (observed against Qwen via
    # LiteLLM).  Resets on every parsed event, so legitimate long tool
    # invocations don't trip it.
    CLAUDE_CODE_IDLE_TIMEOUT: int = 180


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton.

    Call ``get_settings.cache_clear()`` in tests to force reconstruction.
    """
    return Settings()
