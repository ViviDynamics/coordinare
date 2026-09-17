"""Core in-memory domain models for a single performer invocation."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, Field, field_validator, model_validator

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# BackendEvent — normalised activity event emitted by any backend
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"github_pat_[A-Za-z0-9_]{82,}"),          # fine-grained PAT
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                    # classic PAT
    re.compile(r"gho_[A-Za-z0-9]{36}"),                    # OAuth app token
    re.compile(r"ghs_[A-Za-z0-9]{36}"),                    # App installation token
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{90,}"),             # Anthropic API key
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{20,}"),       # Bearer header value
    re.compile(r"AKIA[0-9A-Z]{16}"),                        # AWS access key
    re.compile(r"sk-litellm-[A-Za-z0-9\-_]{20,}"),         # LiteLLM proxy token (073)
]


def _redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class BackendEventType(str, Enum):
    progress = "progress"  # general working update
    tool_use = "tool_use"  # agent called a tool (file read/write, shell, etc.)
    thinking = "thinking"  # internal reasoning (e.g. Claude extended thinking)
    cost     = "cost"      # token / cost accounting
    error    = "error"     # non-fatal error within the session
    output   = "output"    # raw output line (fallback for unrecognised events)


class BackendEvent(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: BackendEventType
    text: str               # human-readable one-line summary (≤ 200 chars)
    detail: str = ""        # optional longer detail (file path, tool args, etc.)
    is_delta: bool = False
    stream_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def _redact_sensitive_detail(cls, data: object) -> object:
        if isinstance(data, dict) and "detail" in data:
            data["detail"] = _redact_secrets(str(data["detail"]))
        return data

if TYPE_CHECKING:
    from performer.backends.base import BackendAdapter

# ---------------------------------------------------------------------------
# Score — the dispatch payload
# ---------------------------------------------------------------------------

# 036: Accept any HTTPS host with exactly owner/repo path (supports GitHub Enterprise Server)
_GITHUB_REPO_RE = re.compile(
    r"^https://[A-Za-z0-9.\-]+(:[0-9]+)?/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+(\.git)?$",
)

# 151 (bench-only): a harness-local `git daemon` remote over git://. Accepted by
# _validate_repo_url ONLY when ALLOW_INSECURE_REPO_URL is set in the environment —
# a relaxation the board-sim benchmark opts into; production defaults stay
# https-only. Unlike _GITHUB_REPO_RE (exactly owner/repo), the path here is
# deliberately one OR two segments: `git daemon` serves a bare repo at the root
# of its base path, so the bench remote is git://host:9418/bench-repo — there is
# no owner segment to require.
_GIT_REPO_RE = re.compile(
    r"^git://[A-Za-z0-9.\-]+(:[0-9]+)?/[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)?(\.git)?$",
)

# 077: free-form viability/benchmark probe role. A diagnostic job runs the
# agent agentically with the persona/task verbatim and full tooling, but with
# NONE of the lifecycle scaffolding — no JSON-verdict contract, no commit/
# push/PR, no env-cache verify or service-inference. It exists so a smoke
# harness (or operator) can answer "can THIS backend actually do X on THIS
# model?" — e.g. drive a headless browser — without routing through a
# distorting lifecycle role. Terminal status: "diagnostic_complete".
DIAGNOSTIC_ROLE = "diagnostic"

# 077: visual evidence must come from driving the application IN THIS
# environment — never a third-party screenshot/URL-rendering service. Observed
# failure: codex (on a large model) "screenshotted" the task URL via Thum.io and
# saved the result, which captures an external/cached page rather than the
# change under test — fabricated QA evidence. Injected into the QA visual-
# evidence contract across backends.
LOCAL_CAPTURE_RULE = (
    "Capture all visual evidence by driving a browser IN THIS environment "
    "(Playwright + Chromium are installed here) against the app running in this "
    "workspace — e.g. the local dev server / build / localhost. Do NOT use any "
    "third-party screenshot or URL-rendering service (Thum.io, screenshotapi, "
    "urlbox, microlink, htmlcsstoimage, etc.): they capture an external or "
    "cached page, not the change under test, and count as fabricated evidence. "
    "If you cannot drive the local app, record that in visual_capture_blockers "
    "rather than substituting an external service."
)


class Score(BaseModel):
    """Dispatch payload received from the coordinare."""

    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    clarifications: list[dict] = Field(default_factory=list)
    # 123 US4 (FR-011): answered Q&A from earlier assessor runs, injected by
    # dispatch_performer on an assessor RE-dispatch so the assessor does not
    # re-ask what a prior bounce already answered. Read by the spec-166
    # assessor intake, which merges it with ``clarifications``. Must be
    # declared here or extra="ignore" drops it in transit and the assessor
    # re-asks every question while the dispatch looks correct.
    prior_clarifications: list[dict] = Field(default_factory=list)
    repo_url: str
    branch: str
    github_token: str = ""
    # Empty string is permitted when the runtime environment supplies a
    # GITHUB_TOKEN env var (e.g. Kubernetes transport where auth is injected
    # via K8s Secrets, or local dev with GITHUB_TOKEN exported).  Always use
    # ``effective_github_token`` for API calls and git auth — it resolves the
    # payload token first and falls back to GITHUB_TOKEN.  If neither is set,
    # GitHub API helpers raise GitHubAPIError(401) rather than sending a
    # malformed ``Authorization: Bearer `` header.
    base_branch: str = ""
    persona_instructions: str = ""  # role-specific behavior instructions
    relay_feedback: list[dict] = Field(default_factory=list)  # human review comments to address
    # 126: implementer-disputed items injected ONLY into the raising stage's
    # dispatch ({id, body, reason}); its verdict adjudicates them.
    disputed_feedback: list[dict] = Field(default_factory=list)
    role: str = "implementing"  # performer stage (implementing, reviewing, security, etc.)
    pr_url: str = ""  # existing PR URL (for reviewer/security/QA roles)
    pr_node_id: str = ""  # existing PR node ID (for terminal status)
    pr_diff: str = ""  # raw unified PR diff injected for review roles (reviewer/closer/qa/tech_writer)
    # 412: why pr_diff is absent — "injected" (present), "empty" (fetched, no
    # reviewable diff), "failed"/"unavailable" (fetch outage). The workflow
    # short-circuits only on a real empty; an outage holds instead.
    pr_diff_status: str = ""
    # 412 round 25: the GitHub-side changed-path list, which survives diff
    # sanitization — the security workflow reads it to distinguish a
    # non-scannable change set (not_applicable) from content it never saw.
    pr_changed_paths: list[str] = Field(default_factory=list)
    # 412 round 39: the dispatch caps the list; overflow means paths were cut,
    # so the security empty-diff classification must hold instead of trusting
    # a partial list.
    pr_changed_paths_overflow: bool = False
    # 164: optional role workflow name. Score uses extra="ignore", so this MUST
    # be declared or the field is silently dropped and the role runs the pre-164
    # single-backend path while looking correctly configured.
    verify_provided: bool = False  # 174: protected bootstrap artifact
    activate_provided: bool = False
    workflow: str = ""
    # 410: card labels ride the dispatch payload so a no-brief implementer run
    # can infer the lane (docs, dependency, config, chore) without a work_kind.
    labels: list[str] = Field(default_factory=list)
    # 173: the project board's node id, for the curator's add-to-board call.
    # Nothing conveyed it before, and the add-to-board mutation silently no-ops
    # without it, so the curator must report an empty value rather than appear
    # to succeed.
    project_id: str = ""
    # 164: operator-supplied env for the role workflow (app start/seed command,
    # PORT). Declared here or extra="ignore" drops it in transit.
    workflow_env: dict[str, str] = Field(default_factory=dict)
    # 164: structured repair brief from the previous QA round (shape mirrors
    # scanner_findings). Reports what failed and how to reproduce; never
    # prescribes a fix.
    qa_findings: list[dict] = Field(default_factory=list)
    repair_mandate: dict[str, Any] | None = None
    scanner_findings: list[dict[str, Any]] = Field(default_factory=list)
    scope_focus: str = ""
    scope_addon: str = ""
    max_tool_calls: int | None = Field(default=None, ge=1, le=500)
    # 165: reader-specific projections of the architect's blueprint, derived by
    # coordinare at dispatch. Each reader gets only its own slice (data-model.md
    # disjointness rule). Declared here or extra="ignore" drops them in transit.
    implementation_brief: dict = Field(default_factory=dict)
    documentation_findings: dict = Field(default_factory=dict)
    completed_documentation: dict = Field(default_factory=dict)
    documenting_side_run: bool | None = None
    documentation_brief: dict = Field(default_factory=dict)
    verification_brief: dict = Field(default_factory=dict)
    # 166: the assessor's structured assessment for one card (goal,
    # expected_behavior, out_of_scope, questions, assumptions, criteria with
    # their source, carried clarifications). Injected into architecting dispatch
    # only (FR-013); absent from all other stages. Must be declared here or
    # extra="ignore" silently drops it.
    assessment: dict[str, Any] | None = None
    # 169: the reviewer's structured findings for one card (changed_files with
    # hunks, findings with anchors, survey commands, dispositions, coverage pass,
    # verdict, post result). Injected into implementing dispatch only (FR-012);
    # absent from reviewing and all other stages. Must be declared here or
    # extra="ignore" silently drops it.
    review_findings: dict[str, Any] | None = None
    # 165: a small blueprint dispatches the implementer for one turn with no
    # milestone loop.
    implementer_single_turn: bool = False
    backend: str = ""  # AI backend override (037)
    model: str = ""  # AI model override (037)
    effort: str = ""  # 055: low/medium/high effort hint for backend
    temperature: float | None = None  # 055: 0.0–1.0; None = backend default
    max_tokens: int | None = None  # 055: output token cap; None = unlimited
    github_api_url: str = ""  # GitHub API URL override (036)
    github_graphql_url: str = ""  # GitHub GraphQL URL override (151)
    architecture_plan_path: str = ""  # path to architect's plan on branch
    issue_number: int = 0  # GitHub issue number for PR linkage
    issue_url: str = ""  # GitHub issue URL for PR body reference
    latest_main_sha: str = ""  # 054: used by QA to verify branch freshness
    env_cache_path: str = ""  # 060: container path of mounted env-cache; sourced via activate.sh
    reasoning_policy: Literal["disable_thinking"] | None = None
    orchestration: dict | None = None  # 080: dual-model proxy block (None = single, no proxy)
    # 089: implementer local-test gate config delivered by dispatch_performer.
    # Must be declared here or extra="ignore" silently drops it (C1). Shape:
    # {enabled: bool, timeout_seconds: int} and, since 409, the optional
    # operator overrides {command, lint_command, test_path_patterns, roots}.
    # None/absent ⇒ gate dormant (SC-005).
    local_test_gate: dict | None = None
    # 409: an operator-pinned test command, read by detect_test_command ahead
    # of detection. Must be declared here or extra="ignore" silently drops it,
    # which is exactly what made the override a dead field.
    test_command: str | None = None
    # 116: whether coordinare manages stateful services for this env_bootstrap. False
    # (the coordinare default) restores performer-owned env setup — the performer skips
    # the 101 service-readiness gate (bootstrap success = the toolchain verify.sh).
    # Must be declared here or extra="ignore" silently drops it; defaults True so an
    # older/synthetic payload preserves the managed path.
    coordinare_manages_services: bool = True
    # 124 (FR-006): documenter mode for the living docs/wiki. "init" = full
    # wiki build (symphony bootstrap); "update" = incremental refresh (the
    # in-card documenting stage, the default). Must be declared here or
    # extra="ignore" silently drops it (CT1); default preserves older payloads.
    doc_mode: Literal["init", "update"] = "update"
    # 124 (C): performer-INTERNAL plan->write decomposition marker. None on the
    # dispatched Score = the PLAN phase (emit the page plan). The documenting
    # handler sets it to {"path","intent","current"} on the per-page write Scores
    # it builds internally, which routes hermes to a minimal single-page WRITE
    # prompt (no diff). Never sent by the coordinare; performer-local only.
    doc_write_target: dict | None = None

    model_config = {"extra": "ignore"}  # silently drop unknown fields from coordinare

    @field_validator("workflow_env", mode="before")
    @classmethod
    def _coerce_workflow_env(cls, value: Any) -> Any:
        """Env vars are strings; accept the scalars YAML naturally produces.

        An operator writes ``PORT: 3000`` and ``QA_APP_BOOT_TIMEOUT: 180`` --
        unquoted, the way everyone writes YAML -- and gets ints. Rejecting them
        makes the natural spelling a validation error. Nested values are still
        refused: a dict or list is not an environment variable.
        """
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("workflow_env must be a mapping of NAME: value")
        out: dict[str, str] = {}
        for k, v in value.items():
            if isinstance(v, (dict, list, tuple, set)):
                raise ValueError(
                    f"workflow_env[{k!r}] must be a scalar; got {type(v).__name__}",
                )
            if isinstance(v, bool):
                v = "1" if v else "0"
            out[str(k)] = "" if v is None else str(v)
        return out

    @field_validator("repo_url")
    @classmethod
    def _validate_repo_url(cls, v: str) -> str:
        if _GITHUB_REPO_RE.match(v):
            return v
        # 151: the board-sim bench clones/pushes a harness-local `git daemon`
        # over git://. That scheme is accepted ONLY when ALLOW_INSECURE_REPO_URL is
        # set in the performer environment (the bench sets it via config.env).
        # Default (unset) stays https-only — production trust boundary unchanged.
        if os.environ.get("ALLOW_INSECURE_REPO_URL") and _GIT_REPO_RE.match(v):
            # Self-announcing: the env var is process-global and otherwise
            # invisible, so a bench config copied into a real deployment would
            # keep accepting git:// silently. This fires only where it is wanted.
            log.warning("score.insecure_repo_url_accepted", repo_url=v)
            return v
        msg = (
            f"repo_url must be an HTTPS URL with owner/repo path "
            f"(https://{{host}}/{{owner}}/{{repo}}[.git]), got: {v!r}"
        )
        raise ValueError(msg)

    @field_validator("branch")
    @classmethod
    def _validate_branch(cls, v: str) -> str:
        if not v.strip():
            msg = "branch must be non-empty"
            raise ValueError(msg)
        if " " in v or ".." in v:
            msg = f"branch contains invalid git ref characters: {v!r}"
            raise ValueError(msg)
        return v

    @property
    def effective_github_token(self) -> str:
        """Resolved GitHub token for API calls and git HTTP auth.

        When ``github_token`` is empty (Kubernetes transport — auth is injected
        into the container via K8s Secrets), falls back to the ``GITHUB_TOKEN``
        environment variable so that git operations and GitHub API calls succeed
        without the coordinare needing to know the Kubernetes secret value.
        """
        return self.github_token.strip() or os.environ.get("GITHUB_TOKEN", "")

    @property
    def owner_repo(self) -> tuple[str, str]:
        """Extract (owner, repo) from the validated GitHub URL."""
        path = self.repo_url.rstrip("/").removesuffix(".git")
        parts = path.split("/")
        return parts[-2], parts[-1]

    @property
    def tool_env(self) -> dict[str, str]:
        """Env vars exported to the backend subprocess so in-container CLI
        shims (e.g. ``performer-upload-screenshot``) can authenticate and
        target the correct repo/issue. Only populated when the relevant
        fields are present; empty values are omitted so the CLI's missing-
        context check fails cleanly rather than sending empty headers.
        """
        env: dict[str, str] = {}
        token = self.effective_github_token
        if token:
            env["PERFORMER_GH_TOKEN"] = token
        try:
            owner, repo = self.owner_repo
        except (IndexError, ValueError):
            owner = repo = ""
        if owner:
            env["PERFORMER_GH_OWNER"] = owner
        if repo:
            env["PERFORMER_GH_REPO"] = repo
        # Prefer PR number when present (QA role), else issue number.
        issue_for_upload = 0
        if self.pr_url:
            # extract trailing /pull/<N>
            import re
            m = re.search(r"/pull/(\d+)", self.pr_url)
            if m:
                issue_for_upload = int(m.group(1))
        if issue_for_upload <= 0 and self.issue_number > 0:
            issue_for_upload = self.issue_number
        if issue_for_upload > 0:
            env["PERFORMER_GH_ISSUE"] = str(issue_for_upload)
        return env


# ---------------------------------------------------------------------------
# Stand — the ephemeral workspace
# ---------------------------------------------------------------------------

PerformanceState = Literal[
    "accepted", "working", "blocked", "pr_opened", "plan_committed",
    "approved", "changes_requested",
    "security_passed", "security_failed",
    "nothing_to_review", "nothing_to_scan", "not_applicable",
    "qa_passed", "qa_failed", "qa_env_blocked",
    "diagnostic_complete",
    "docs_committed",
    "assessment_complete",
    "error", "session_expired", "waiting_for_checks",
]


@dataclass
class Stand:
    """Ephemeral local workspace created for a single performance."""

    path: Path
    branch: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    git_env: dict[str, str] = field(default_factory=dict)
    cache_env: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Performance — the in-memory session
# ---------------------------------------------------------------------------

@dataclass
class Performance:
    """Represents one complete end-to-end performer session."""

    session_id: str
    stand: Stand
    score: Score
    backend: "BackendAdapter"
    # 080: in-container dual-model proxy for this job (None for single-mode); held
    # so it shares the backend's lifecycle and is stopped at session teardown.
    orchestration_proxy: Any = None
    state: PerformanceState = "accepted"
    role: str = "implementing"  # 020: performer role (e.g. "implementing", "architecting")
    pr_url: str | None = None
    pr_node_id: str | None = None
    pr_head_sha: str | None = None
    # 070: branch HEAD captured at session start so the coordinare can detect
    # implementer turns that produced zero commits and route them to retry
    # instead of accepting a no-progress "blocked" verdict at face value.
    head_at_start: str | None = None
    # 072: pre-turn count of PR comments (any author) captured at dispatch.
    # Used to compute ``bot_pr_comment_delta`` on terminal responses for the
    # coordinare per-role zero-progress guardrail.
    pr_comments_at_start: int | None = None
    plan_path: str | None = None  # 020: path to committed architecture plan
    check_attempt: int = 0
    # 065 Fix 14: track last failure signature so we can bail when the same
    # checks fail in the same way two attempts in a row (model not making progress).
    last_check_failure_signature: str | None = None
    check_no_progress_streak: int = 0
    review_comments: list[dict] = field(default_factory=list)  # 021: [{file, line, body}]
    review_suggestions: list[str] = field(default_factory=list)  # 021: non-blocking suggestions
    review_cycle: int = 0  # 021: number of review cycles exhausted
    security_findings: list[dict] = field(default_factory=list)  # 022: [{severity, category, ...}]
    security_cycle: int = 0  # 022: number of security fix cycles
    qa_failures: list[dict] = field(default_factory=list)  # 023: [{criterion, expected, actual, test}]
    qa_new_tests: list[str] = field(default_factory=list)  # 023: paths of committed test files
    qa_report: dict | None = None  # 023: pass report {criteria_checked, criteria_passed, new_tests_added}
    qa_cycle: int = 0  # 023: number of QA fix cycles
    docs_files_modified: list[str] = field(default_factory=list)  # 024: doc files committed
    # 124 (C): performer-internal plan->write decomposition state for the
    # documenting stage. doc_phase is None during the PLAN run, "writing" once the
    # plan is parsed; the queue drains one page per backend run; pending files +
    # planned deletions are committed in one batch when the queue empties.
    doc_phase: str | None = None
    doc_write_queue: list[dict] = field(default_factory=list)  # remaining pages [{path,intent}]
    doc_deletions: list[str] = field(default_factory=list)  # planned page retirements
    doc_files_pending: list[dict] = field(default_factory=list)  # accumulated {path,content}
    # 063 T026c: cached service-inference outcome from env_bootstrap so resume
    # paths replay it without re-running the (expensive) LLM agent.
    inference_state: dict[str, object] = field(default_factory=dict)
    assessment_questions: list[str] = field(default_factory=list)  # assessor: questions when insufficient
    open_questions: list[str] = field(default_factory=list)
    # 045: Count of backend-output parse retries used in this session (across
    # assess/review/security/qa/docs).  Bounded by Settings.BACKEND_PARSE_RETRIES.
    parse_retry_count: int = 0
    error_reason: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
