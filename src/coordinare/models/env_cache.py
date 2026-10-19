"""Models for performer environment caching (spec 060)."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — needed at runtime for pydantic field resolution
from pathlib import Path  # noqa: TC003 — needed at runtime for pydantic field resolution
from typing import Any, Literal

from pydantic import BaseModel, Field

# 265: bound on a daemon-rendered script carried in the bootstrap dispatch. Matches the performer's bound.
RENDERED_SCRIPT_MAX_CHARS = 256 * 1024
# 265: the verify basis a bootstrap reports when the performer verified the daemon's rendered text.
ENV_VERIFY_BASIS_DAEMON_RENDERED = "daemon_rendered"


class EnvCacheState(BaseModel):
    """Live tracking of one symphony's env-cache bootstrapping status."""

    symphony_name: str
    sanitised_name: str
    cache_dir: Path

    readme_sha: str | None = None
    bootstrap_in_flight: bool = False
    manifest_items: list[dict[str, Any]] = Field(default_factory=list)
    environment_attempt: dict[str, Any] | None = None
    environment_attempt_archive: list[dict[str, Any]] = Field(default_factory=list, max_length=3)
    environment_capabilities: dict[str, Any] | None = None
    environment_runtime_artifacts: dict[str, Any] | None = None
    environment_source_owner: str | None = None
    environment_execution_source: dict[str, Any] | None = None
    environment_manifest: dict[str, Any] | None = None
    environment_assessment: dict[str, Any] | None = None
    environment_verification: dict[str, Any] | None = None
    pending_sha: str | None = None
    # 400: set while a FORCED regen is in flight, with the manifest SHA it was
    # built against. Transient like bootstrap_in_flight (not persisted): a
    # restart mid-run falls back to the old two-bootstrap behaviour, never to
    # a stale cache.
    bootstrap_forced: bool = False
    forced_manifest_sha: str | None = None
    # 077: the most recent combined spec-file SHA observed by check_and_trigger
    # (refreshed every cycle, even when no bootstrap is needed). Consumer dispatch
    # gates on ``readme_sha == last_seen_spec_sha`` so a card never runs against a
    # cache that predates the current spec (e.g. a README that just added Chrome).
    # Live/derived — re-populated within one poll cycle, so not persisted.
    last_seen_spec_sha: str | None = None
    last_bootstrap_at: datetime | None = None
    last_bootstrap_succeeded: bool | None = None
    # 077: human-readable reason the last bootstrap FAILED (verify output, dispatch
    # error, reap reason, …). Set on failure, cleared on success. Surfaced on the
    # dashboard so an operator can see WHY a symphony's env bootstrap is failing.
    last_bootstrap_error: str | None = None
    cache_dir_ready: bool = False
    # 218: the authoritative verify.sh and activate.sh TEXT the daemon rendered for the
    # current bootstrap, kept daemon-side (persisted in the snapshot). The copies in the
    # cache are performer-writable and are never trusted: verification runs this text on
    # stdin, and a mount or dispatch gate requires activate.sh to match it byte for byte.
    # None means no daemon-owned script is on record, so nothing verifies or mounts.
    bootstrap_commit_sha: str | None = None
    rendered_verify_sh: str | None = None
    rendered_activate_sh: str | None = None
    # 218 (review round 3): infrastructure failures (docker could not run the verify, or the bootstrap
    # container could not be proven quiet). A separate budget from the verdict budget; reset on success.
    infra_failures: int = 0
    last_infra_failure_at: datetime | None = None
    # 234: sha256 of the daemon-owned provenance manifest frozen after the last verified bootstrap, and when.
    # None means nothing is frozen: consumer dispatch is refused until a re-bootstrap freezes one.
    provenance_digest: str | None = None
    provenance_frozen_at: datetime | None = None
    # 265: Kubernetes stores the bounded consumer inputs and digests, with no manifest file.
    provenance_stat_digest: str | None = None
    provenance_inputs: dict[str, Any] | None = None
    provenance_entry_count: int = 0
    provenance_total_bytes: int = 0
    verifier_provenance: dict[str, Any] | None = None
    # Confirmed stat refusals since the last consumer success or bootstrap freeze; persisted across restart.
    consumer_stat_refusals: int = Field(default=0, ge=0)
    # Keep the refusal budget through successful repair bootstraps until the spec changes.
    runtime_repair_budget: bool = False
    bootstrap_budget_sha: str | None = None

    # 088 (FR-009/FR-010): bootstrap circuit breaker. ``bootstrap_attempts``
    # counts consecutive failures for the current spec SHA; reset on success
    # and on SHA change. ``bootstrap_exhausted`` is the terminal breaker state:
    # dispatch stops, consumer holds name the exhaustion, and only a SHA change
    # (spec fix) or a successful bootstrap re-arms it. Both persist in the
    # snapshot so a restart does not hand out a fresh budget.
    bootstrap_attempts: int = 0
    bootstrap_exhausted: bool = False
    # Configuration refusals alone disable consumer rechecks until the breaker is reset.
    bootstrap_exhaustion_reason: Literal["configuration_refusal"] | None = None

    # 063 Phase 4 (T024): set by EnvCacheService.mark_runtime_health_failed when
    # a performer reports a non-zero services-health.sh exit. Causes the next
    # check_and_trigger cycle to dispatch a forced regeneration that bypasses
    # the cache_inputs key entirely, then clears the flag.
    runtime_health_failed: bool = False

    # 063 Cross-cutting (T026d): summary of the most recent service-inference
    # outcome from the env_bootstrap performer. Populated by
    # ``EnvCacheService.record_inference_outcome`` when a bootstrap job
    # finishes (terminal status) so the dashboard can surface what the agent
    # produced. ``last_inference_skipped_reason`` is non-None when the agent
    # did not run (no env_cache_path, coordinare package missing, manual
    # override applied, no API key, unexpected error).
    last_inference_at: datetime | None = None
    last_inference_skipped_reason: str | None = None
    last_inference_agent_version: str | None = None
    last_inference_attempts: int | None = None
    last_inference_succeeded: bool | None = None
    last_inference_services: list[str] = Field(default_factory=list)

    # 092 (FR-016/FR-017): the test-env file PATH discovered by the inference
    # agent (ServicesManifest.test_env_source), captured at record-inference time
    # when no symphony-level test_env config block is set. PATH ONLY — never the
    # loaded KEY=VALUE pairs. Persisted onto the snapshot so a reused cache
    # reloads variables from this source file at QA/performer runtime instead of
    # carrying baked-in secret values.
    test_env_source: str | None = None

    # 124 (US2): living docs/wiki initialization gate state, co-located here
    # (persisted subset mirrors EnvCacheStateSnapshot; wiki_in_flight is transient,
    # reset on restart like bootstrap_in_flight). wiki_initialized is the durable
    # marker that the seed wiki has merged to the default branch.
    wiki_initialized: bool = False
    wiki_in_flight: bool = False  # transient — a wiki-init job is mid-run
    wiki_attempts: int = 0
    wiki_exhausted: bool = False
    last_wiki_init_at: datetime | None = None
    last_wiki_init_succeeded: bool | None = None
    last_wiki_init_error: str | None = None

    # 173: the two card-less intake roles (advocate, curator).  Same shape as
    # the wiki-init gate above, one block per role.  ``*_in_flight`` is
    # transient and absent from the snapshot for the same reason
    # ``bootstrap_in_flight`` is: a crash mid-run must not leave a marker on
    # disk that blocks the role forever.
    advocate_in_flight: bool = False
    advocate_attempts: int = 0
    advocate_exhausted: bool = False
    last_advocate_run_at: datetime | None = None
    last_advocate_succeeded: bool | None = None
    last_advocate_error: str | None = None
    last_advocate_issues_seen: int = 0

    curator_in_flight: bool = False
    curator_attempts: int = 0
    curator_exhausted: bool = False
    last_curator_run_at: datetime | None = None
    last_curator_succeeded: bool | None = None
    last_curator_error: str | None = None
    last_curator_issues_seen: int = 0


class BootstrapJobPayload(BaseModel):
    """Dispatch payload for an env_bootstrap performer job."""

    job_type: Literal["env_bootstrap"] = "env_bootstrap"

    symphony_name: str = Field(
        description="Human-readable symphony name (for logging/labelling inside the container).",
    )
    symphony_org: str = Field(description="GitHub organisation owning the symphony's repo.")
    symphony_repo: str = Field(description="GitHub repository name for the symphony.")

    env_spec_files: list[str] = Field(
        default_factory=lambda: ["README.md"],
        description="Relative paths of the files whose content describes the dev environment.",
    )
    env_spec_contents: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of file path → full text content at the time of the SHA change.",
    )

    cache_mount_path: str = Field(
        default="/devenv/symphony",
        description=(
            "Full in-container path to the symphony's env cache subdirectory, e.g. "
            "'/devenv/my-project-a1b2c3'. Coordinare sets this from "
            "'{container_devenv_root}/{sanitised_symphony_name}'. The default is "
            "illustrative; the actual value is always computed by coordinare from config."
        ),
    )
    last_failure: str | None = Field(
        default=None,
        description=(
            "077: the previous bootstrap's verification failure (verify.sh output / "
            "error reason), injected into the persona on a RETRY so the agent can "
            "fix the specific issue (e.g. a pinned Ruby version that wasn't installed) "
            "instead of repeating the same miss."
        ),
    )
    environment_execution_source: dict[str, Any] | None = None
    environment_manifest: dict[str, Any] | None = None
    environment_assessment: dict[str, Any] | None = None
    environment_verification: dict[str, Any] | None = None
    environment_manifest_digest: str | None = None
    environment_verification_image: str | None = None
    environment_verification_platform: str | None = None
    environment_runtime_artifacts: dict[str, Any] | None = None
    environment_attempt_expires_at: float | None = None
    dependency_checklist: str | None = Field(
        default=None,
        description=(
            "077: coordinare-derived authoritative install checklist (from the manifest). "
            "Injected into the persona so the agent installs EXACTLY these dependencies "
            "with their pinned versions, instead of free-forming from the README."
        ),
    )
    verify_provided: bool = Field(
        default=False,
        description=(
            "077: True when coordinare has written an authoritative verify.sh into the "
            "cache from the manifest. The agent must RUN it and make it pass, and must "
            "NOT create/overwrite it (coordinare owns the verification contract)."
        ),
    )
    activate_provided: bool = Field(
        default=False,
        description=(
            "087: True when coordinare has written an authoritative, auto-discovering "
            "activate.sh into the cache from the manifest. The agent installs the pinned "
            "toolchain but must NOT create/overwrite activate.sh — coordinare owns the "
            "activation contract. Hand-written activation paths (.rbenv vs rbenv, .nvm "
            "vs nvm) were fumbled every run, leaving a built cache verify.sh couldn't see."
        ),
    )
    declared_services: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "091: durably-declared stateful services (from .coordinare/score.json), each a "
            "ServiceEntry dump. A service with a coordinare-known kind (postgres, redis) makes "
            "the bootstrap fetch its binary as a .deb into <cache>/debs/ via the existing "
            "system-package path, so the cache contains the service binary while the base "
            "image stays agnostic. Empty when no stateful service is declared (behavior "
            "unchanged)."
        ),
    )
    test_env_vars: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "092: symphony test-environment variables (literal KEY=VALUE pairs) loaded by "
            "coordinare from the configured test_env source. Threaded into the bootstrap "
            "performer's process env so the service-inference start-phase dry-run (which runs "
            "inside this performer) sees them and clears the unset-secret gate. These are "
            "SECRET-LIKE: they travel in the transient dispatch payload but MUST be redacted "
            "in logs (names + source only, never values) per the spec-091/092 invariant. "
            "Empty when no test_env is configured/discovered (behavior unchanged)."
        ),
    )
    coordinare_manages_services: bool = Field(
        default=True,
        description=(
            "116: whether coordinare manages stateful services for this bootstrap (the 091→115 "
            "subsystem: deb-fetch persona, rendered service scripts, the 101 readiness gate). "
            "Sourced from EnvCacheConfig.coordinare_manages_services. When False (the default "
            "config), the env-bootstrap PERFORMER owns env setup end-to-end and skips the "
            "coordinare readiness gate; declared_services is empty so no service persona block "
            "is injected. Defaults True on the model so an older/synthetic payload preserves "
            "the managed path; coordinare always sets it explicitly from config."
        ),
    )
    rendered_verify_sh: str | None = Field(
        default=None,
        max_length=RENDERED_SCRIPT_MAX_CHARS,
        description=(
            "265: the daemon-rendered verify.sh text. The bootstrap pod cannot see the daemon's cache on "
            "Kubernetes, so the text rides the dispatch. The performer writes it into the cache at job start and "
            "runs it (not the agent's copy) to decide success. None when no manifest was rendered."
        ),
    )
    rendered_activate_sh: str | None = Field(
        default=None,
        max_length=RENDERED_SCRIPT_MAX_CHARS,
        description=(
            "265: the daemon-rendered activate.sh text, carried with rendered_verify_sh and written into the "
            "cache at job start, overwriting the agent's copy."
        ),
    )
