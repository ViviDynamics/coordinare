"""HTTP-mode adapter implementing AgentServiceProtocol (spec 056, T027).

Wraps :class:`~coordinare.transport.http_transport.PerformerHTTPClient` and the
container lifecycle service so the existing dispatch/monitor flow can route
ephemeral and persistent containerized performers through the same call sites
as the subprocess transport.

Mode handling:

* ``persistent`` — ``endpoint`` is fixed at registration; ``dispatch_card``
  sends ``POST /jobs`` directly.
* ``ephemeral``  — ``dispatch_card`` first calls
  :func:`performer_lifecycle.start_ephemeral` + ``wait_ready`` to acquire a
  one-shot container, then dispatches. The container_id is recorded so
  :meth:`check_status` can call :func:`performer_lifecycle.stop` when the
  job reaches a terminal state, regardless of success/failure (FR-003).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.nodes.handle_system_error import classify_upstream
from coordinare.services import performer_lifecycle
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import (
    PerformerAuthError,
    PerformerHTTPClient,
    PerformerUnreachableError,
)
from coordinare.upstream_errors import UpstreamHTTPError, strip_base_url_credentials

if TYPE_CHECKING:
    from coordinare.models.performer_endpoint import (
        JobInitPayload,
        PerformerEndpointConfig,
        VolumeMount,
    )
    from coordinare.workspace import WorkspaceInfo

logger = structlog.get_logger(__name__)


_TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled"}


@dataclass
class _EphemeralJob:
    """State for one in-flight ephemeral container job."""
    container_id: str
    endpoint: str
    client: PerformerHTTPClient


class HTTPPerformerService:
    """AgentService-compatible adapter for ephemeral / persistent performers."""

    def __init__(
        self,
        config: PerformerEndpointConfig,
        *,
        client: PerformerHTTPClient | None = None,
    ) -> None:
        self._config = config
        self._injected_client = client
        # Persistent: endpoint is known at construction time.
        # Ephemeral:  per-job state tracked in _active_jobs keyed by job_id.
        self._persistent_endpoint: str | None = (
            str(config.endpoint).rstrip("/") if config.endpoint is not None else None
        )
        self._persistent_client: PerformerHTTPClient | None = client
        # Ephemeral jobs: job_id → _EphemeralJob (replaces shared _container_id/_endpoint/_client)
        self._active_jobs: dict[str, _EphemeralJob] = {}
        # Serialise concurrent dispatch_card calls: a performer registered in
        # multiple stage pools (multi-role config) must not accept two jobs at once.
        self._dispatch_lock = asyncio.Lock()
        # Rolling buffer of container stdout+stderr lines for the active job,
        # refreshed every ~5 s by _poll_container_logs(). Readable via get_agent_logs().
        self._log_buffer: list[str] = []
        # Per-job background poll tasks: job_id → Task (mirrors _active_jobs).
        self._log_poll_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def mode(self) -> str:
        return self._config.mode

    @property
    def devenv_root(self) -> str:
        return self._config.container_devenv_root

    def _auth_token(self) -> str | None:
        if self._config.auth_token is None:
            return None
        return self._config.auth_token.get_secret_value()

    def has_live_session(self, session_id: str) -> bool:
        """Return True if this service can still reach the container for ``session_id``.

        Ephemeral: a job is live only while the per-container client is tracked
        in ``_active_jobs`` — after a daemon restart the dict is empty and any
        snapshot-restored session_id refers to a container the new process
        cannot reach.

        Persistent: there is one shared endpoint; "live" simply means the
        endpoint URL is resolved.
        """
        if self._injected_client is not None:
            return True
        if self._config.mode == "ephemeral":
            return session_id in self._active_jobs
        return self._persistent_endpoint is not None

    def _ensure_client(self, job_id: str | None = None) -> PerformerHTTPClient:
        if self._injected_client is not None:
            return self._injected_client
        # Ephemeral: look up per-job client.
        if self._config.mode == "ephemeral" and job_id is not None:
            job = self._active_jobs.get(job_id)
            if job is not None:
                return job.client
            # 069 diagnostic: ephemeral lookup miss — about to fall through to
            # the persistent branch, which has no endpoint for ephemeral configs
            # and will raise TransportError. Log instance identity + known job_ids
            # so we can tell whether the polling caller resolved to a different
            # service instance than the one that ran dispatch_card.
            logger.warning(
                "http_performer.ephemeral_job_lookup_miss",
                performer_id=self._config.id,
                requested_job_id=job_id,
                known_job_ids=list(self._active_jobs.keys()),
                service_instance_id=id(self),
            )
        # Persistent: use shared client.
        if self._persistent_client is None:
            if self._persistent_endpoint is None:
                raise TransportError(
                    f"performer {self._config.id} has no endpoint resolved yet"
                )
            self._persistent_client = PerformerHTTPClient(
                self._persistent_endpoint,
                auth_token=self._auth_token(),
            )
        return self._persistent_client

    async def aclose(self) -> None:
        if self._persistent_client is not None and self._injected_client is None:
            await self._persistent_client.aclose()
            self._persistent_client = None
        # Cancel poll tasks first so they don't race against container teardown.
        for task in self._log_poll_tasks.values():
            task.cancel()
        self._log_poll_tasks.clear()
        # Cancel any in-flight ephemeral containers so they don't leak on shutdown.
        active = list(self._active_jobs.values())
        self._active_jobs.clear()
        for job in active:
            await self._cleanup_ephemeral_job(job)

    # ------------------------------------------------------------------
    # AgentServiceProtocol
    # ------------------------------------------------------------------
    async def check_health(self) -> dict[str, Any]:
        # Ephemeral performers have no persistent endpoint to probe — concurrency
        # is governed by SlotManager (checked before health), so always report idle.
        if self._config.mode == "ephemeral" and self._persistent_endpoint is None:
            return {"status": "idle", "availability": "idle"}
        try:
            client = self._ensure_client()
            status = await client.get_status()
        except PerformerAuthError as exc:
            logger.warning("http_performer.auth_failed", performer_id=self._config.id, error=str(exc))
            return {"status": "error", "reason": str(exc)}
        except PerformerUnreachableError as exc:
            logger.warning(
                "http_performer.unreachable",
                performer_id=self._config.id,
                endpoint=self._persistent_endpoint,
                error=str(exc),
            )
            return {"status": "unreachable", "reason": str(exc)}
        except Exception as exc:  # pragma: no cover — defensive
            return {"status": "unknown", "reason": str(exc)}
        # Map performer availability → AgentService health status.
        availability = status.availability
        caps = status.capabilities.model_dump(mode="json")
        if availability in {"idle", "busy"}:
            return {"status": "idle", "availability": availability, "capabilities": caps}
        if availability == "starting":
            return {"status": "unknown", "availability": availability, "capabilities": caps}
        if availability == "draining":
            return {"status": "draining", "availability": availability, "capabilities": caps}
        return {"status": "unknown", "availability": availability, "capabilities": caps}  # pragma: no cover — PerformerStatus.availability is Literal["starting","idle","busy","draining"], all handled above

    async def dispatch_card(
        self,
        card_context: dict[str, Any],
        workspace_info: WorkspaceInfo | None = None,
        extra_volumes: list[VolumeMount] | None = None,
    ) -> dict[str, Any]:
        async with self._dispatch_lock:
            return await self._dispatch_card_locked(card_context, workspace_info, extra_volumes)

    async def _dispatch_card_locked(
        self,
        card_context: dict[str, Any],
        workspace_info: WorkspaceInfo | None = None,
        extra_volumes: list[VolumeMount] | None = None,
    ) -> dict[str, Any]:
        ephemeral_job: _EphemeralJob | None = None

        # Resolve the effective config: if extra_volumes are provided, create a
        # shallow copy with the updated volumes list (containerized performers only).
        effective_config = self._config
        if extra_volumes and self._config.mode != "subprocess":
            effective_config = self._config.model_copy(
                update={"volumes": list(self._config.volumes) + list(extra_volumes)}
            )

        # Ephemeral mode: spin up a fresh container before dispatching.
        if self._config.mode == "ephemeral":
            try:
                started = await performer_lifecycle.start_ephemeral(effective_config)
            except performer_lifecycle.LifecycleError as exc:
                logger.warning(
                    "http_performer.start_failed",
                    performer_id=self._config.id,
                    error=str(exc),
                )
                return {"status": "error", "reason": f"container start failed: {exc}"}

            # Use the injected client if present (tests); otherwise create one per container.
            ep_client = self._injected_client or PerformerHTTPClient(
                started.endpoint,
                auth_token=self._auth_token(),
            )
            ephemeral_job = _EphemeralJob(
                container_id=started.container_id,
                endpoint=started.endpoint,
                client=ep_client,
            )
            try:
                await performer_lifecycle.wait_ready(
                    started.endpoint,
                    self._auth_token(),
                    timeout=float(self._config.readiness_timeout_s),
                    performer_id=self._config.id,
                )
            except performer_lifecycle.ReadinessTimeoutError as exc:
                await self._cleanup_ephemeral_job(ephemeral_job)
                return {"status": "error", "reason": f"readiness timeout: {exc}"}

        # Build the job-init payload.
        try:
            payload = self._build_job_payload(card_context, workspace_info)
        except ValueError as exc:
            if ephemeral_job is not None:
                await self._cleanup_ephemeral_job(ephemeral_job)
            return {"status": "error", "reason": str(exc)}

        try:
            client = ephemeral_job.client if ephemeral_job is not None else self._ensure_client()
            response = await client.post_job(payload)
        except (PerformerAuthError, PerformerUnreachableError, TransportError) as exc:
            if ephemeral_job is not None:
                await self._cleanup_ephemeral_job(ephemeral_job)
            logger.warning(
                "http_performer.dispatch_failed",
                performer_id=self._config.id,
                error=str(exc),
            )
            return {"status": "error", "reason": str(exc)}

        # 409 busy → surface as error so the dispatch loop retries next cycle.
        if not getattr(response, "accepted", False):
            reason = getattr(response, "reason", "busy")
            detail = getattr(response, "detail", None)
            if ephemeral_job is not None:
                await self._cleanup_ephemeral_job(ephemeral_job)
            return {
                "status": "error",
                "reason": f"performer busy ({reason})" + (f": {detail}" if detail else ""),
            }

        job_id = response.job_id
        if ephemeral_job is not None:
            self._active_jobs[job_id] = ephemeral_job
            self._log_buffer = []
            self._log_poll_tasks[job_id] = asyncio.create_task(
                self._poll_container_logs(ephemeral_job.container_id, job_id),
                name=f"log_poll_{job_id[:8]}",
            )
        logger.info(
            "performer_endpoint.transition",
            performer_id=self._config.id,
            from_state="idle",
            to_state="busy",
            job_id=job_id,
        )
        return {
            "status": "ok",
            "session_id": job_id,
            "job_id": job_id,
            "accepted": True,
            "container_id": ephemeral_job.container_id if ephemeral_job is not None else None,
        }

    async def check_status(
        self,
        session_id: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            client = self._ensure_client(job_id=session_id)
        except TransportError:
            # No active job for this session_id — container was already cleaned up
            # or the session predates this coordinare instance. Re-raise so
            # monitor_performer can route through its transport-error retry path.
            raise
        try:
            status = await client.get_job(session_id)
        except (PerformerAuthError, PerformerUnreachableError, TransportError):
            # Auth failure, unreachability, or transport error — clean up the
            # stale _active_jobs entry so subsequent check_health() calls aren't
            # blocked by the dead container.
            if self._config.mode == "ephemeral":
                await self._cleanup_ephemeral_job_by_id(session_id)
            raise
        is_terminal = status.state in _TERMINAL_JOB_STATES
        if is_terminal:
            logger.info(
                "performer_endpoint.transition",
                performer_id=self._config.id,
                from_state="busy",
                to_state="idle",
                job_id=session_id,
                terminal_state=status.state,
            )
            if self._config.mode == "ephemeral":
                await self._cleanup_ephemeral_job_by_id(session_id)
            elif self._config.mode == "persistent":
                await self.call_reset()

        # Translate JobStatus → PerformerResponse-style dict that monitor_performer.py
        # expects (it reads `status.get("status", "working")`).
        if is_terminal and status.result is not None and status.result.summary:
            # _perform_job serialises the full PerformerResponse as JSON in summary.
            try:
                parsed = json.loads(status.result.summary)
                # Ensure parsed response includes JobState for job lifecycle tracking.
                if "state" not in parsed:
                    parsed["state"] = status.state
                self._log_upstream_http_error(parsed)
                return parsed
            except (json.JSONDecodeError, ValueError):
                pass
            # Plain-text summary fallback: always "error" so monitor_performer can
            # terminate the session (it doesn't recognise plain-text responses).
            return {"status": "error", "reason": status.result.summary, "state": status.state}

        if not is_terminal:
            result: dict[str, Any] = {"status": "working", "job_id": session_id, "job_state": status.state}
            if status.events:
                result["events"] = status.events
            if status.metrics is not None:
                result["metrics"] = status.metrics
            return result

        # Terminal but no result (cancelled or internal failure).
        return {"status": "error", "reason": f"job ended in state '{status.state}' with no result", "job_id": session_id, "state": status.state}

    def _log_upstream_http_error(self, response: dict[str, Any]) -> None:
        """Emit verbatim upstream-body log line when the performer reports a
        non-2xx envelope (spec 067 FR-004). One structlog line per envelope at
        WARN (transient) or ERROR (permanent), kv-ordered per data-model.md §2.
        """
        metrics = response.get("metrics")
        envelope: dict[str, Any] | None = None
        if isinstance(metrics, dict):
            raw = metrics.get("upstream_http_error")
            if isinstance(raw, dict):
                envelope = raw
        if envelope is None:
            return
        try:
            error = UpstreamHTTPError(**envelope)
        except Exception as exc:  # malformed envelope — log once and move on
            logger.warning(
                "http_performer.upstream_envelope_malformed",
                performer_id=self._config.id,
                error=str(exc),
            )
            return
        verdict = classify_upstream(error)
        log_fn = logger.error if verdict == "permanent" else logger.warning
        log_fn(
            "http_performer.upstream_http_error",
            status=error.status,
            route=error.route,
            base_url=strip_base_url_credentials(error.base_url),
            upstream_body=error.body,
            body_truncated=error.body_truncated,
            elapsed_ms=error.elapsed_ms,
            upstream_request_id=error.upstream_request_id,
            performer_id=self._config.id,
            classification=verdict,
        )

    async def call_reset(self) -> bool:
        """POST /reset to the persistent performer endpoint.

        Returns True on 2xx, False (with a warning log) on any non-2xx or
        network error. Never raises — caller can always proceed with next
        dispatch regardless of reset outcome.
        """
        if self._config.mode != "persistent":
            return True
        try:
            client = self._ensure_client()
            resp = await client.post_reset()
            if resp.is_success:
                logger.info(
                    "http_performer.reset_ok",
                    performer_id=self._config.id,
                )
                return True
            logger.warning(
                "http_performer.reset_failed",
                performer_id=self._config.id,
                status_code=resp.status_code,
            )
            return False
        except Exception as exc:
            logger.warning(
                "http_performer.reset_error",
                performer_id=self._config.id,
                error=str(exc),
            )
            return False

    async def relay_feedback(self, review_payload: dict[str, Any]) -> dict[str, Any]:
        # Containerized performers receive feedback as part of the next
        # dispatch payload (card_context["relay_feedback"]).  The wire
        # protocol has no in-flight feedback channel.
        logger.warning(
            "http_performer.relay_feedback_buffered",
            performer_id=self._config.id,
            comments=len(review_payload.get("comments", [])),
        )
        return {"status": "ok", "buffered": True}

    def get_agent_logs(self) -> list[str]:
        """Return buffered stdout+stderr lines from the active ephemeral container.

        Lines are refreshed every ~5 seconds by a background poller started when a
        job is dispatched. Returns an empty list if no container is active.
        """
        return list(self._log_buffer)

    async def _poll_container_logs(self, container_id: str, job_id: str) -> None:
        """Background task: poll `docker logs` every 5 seconds until the job ends."""
        while job_id in self._active_jobs:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "logs", "--tail", "200", container_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
                lines = stdout.decode(errors="replace").splitlines()
                self._log_buffer = lines[-200:]
            except Exception as exc:  # pragma: no cover — best-effort
                logger.debug(
                    "http_performer.log_poll_failed",
                    performer_id=self._config.id,
                    container_id=container_id,
                    error=str(exc),
                )
            await asyncio.sleep(5.0)
        # One final capture after job completes
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "logs", "--tail", "200", container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            self._log_buffer = stdout.decode(errors="replace").splitlines()[-200:]
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _cleanup_ephemeral_job(self, job: _EphemeralJob) -> None:
        """Stop and release a specific ephemeral container + its client."""
        host_log_dir = None
        for vol in self._config.volumes:
            if str(vol.container_path) == "/var/log/performer":
                host_log_dir = vol.host_path
                break
        try:
            await performer_lifecycle.stop(
                job.container_id,
                host_log_dir=host_log_dir,
                performer_id=self._config.id,
            )
        except Exception as exc:  # pragma: no cover — best effort
            logger.warning(
                "http_performer.stop_failed",
                performer_id=self._config.id,
                container_id=job.container_id,
                error=str(exc),
            )
        if self._injected_client is None:
            with contextlib.suppress(Exception):
                await job.client.aclose()

    async def _cleanup_ephemeral_job_by_id(self, job_id: str) -> None:
        """Look up and clean up an ephemeral job by its job_id."""
        task = self._log_poll_tasks.pop(job_id, None)
        if task is not None:
            task.cancel()
        job = self._active_jobs.pop(job_id, None)
        if job is not None:
            await self._cleanup_ephemeral_job(job)

    def _build_job_payload(
        self,
        card_context: dict[str, Any],
        workspace_info: WorkspaceInfo | None,
    ) -> JobInitPayload:
        from coordinare.models.performer_endpoint import JobInitPayload

        # 060/Option A: env_bootstrap dispatches do not flow through
        # WorkspaceManager (no card, no workspace prep). Synthesize the
        # workspace fields from BootstrapJobPayload so the performer's
        # standard clone+dispatch flow can pick up the symphony repo and
        # run install steps from env_spec_contents into cache_mount_path.
        if card_context.get("job_type") == "env_bootstrap":
            return self._build_env_bootstrap_payload(card_context)

        repo_url = (
            str(workspace_info.repo_url)
            if workspace_info is not None and workspace_info.repo_url
            else str(card_context.get("repo_url") or "")
        )
        branch = (
            workspace_info.branch
            if workspace_info is not None and workspace_info.branch
            else str(card_context.get("branch") or "")
        )
        if not repo_url or not branch:
            raise ValueError("workspace context missing repo_url or branch")

        import os

        secrets: dict[str, str] = {}
        if workspace_info is not None and workspace_info.github_token:
            secrets["GITHUB_TOKEN"] = workspace_info.github_token
        # Only inject the API key(s) required by the selected backend to avoid
        # leaking unrelated credentials into container environments.
        # opencode/junie/cursor read provider keys from env natively (no special
        # CLI flag needed), but still need secrets injected here so they reach
        # the subprocess when operators use the HTTP-payload secret path instead
        # of Docker env vars.
        backend = str(card_context.get("backend", "")).replace("-", "_")
        # Per-role overrides plumbed from PerformerRoleConfig via dispatch_performer.
        # base_url + api_key_env let an operator point a backend at a proxy
        # (e.g. LiteLLM) instead of the vendor's native endpoint.
        role_base_url = card_context.get("base_url")
        role_api_key_env = card_context.get("api_key_env")
        if backend == "codex":
            key_env_name = str(role_api_key_env) if role_api_key_env else "OPENAI_API_KEY"
            openai_key = os.environ.get(key_env_name, "")
            if openai_key:
                secrets["OPENAI_API_KEY"] = openai_key
            if role_base_url:
                secrets["OPENAI_BASE_URL"] = str(role_base_url)
        elif backend == "claude_code":
            key_env_name = str(role_api_key_env) if role_api_key_env else "ANTHROPIC_API_KEY"
            anthropic_key = os.environ.get(key_env_name, "")
            if anthropic_key:
                secrets["ANTHROPIC_API_KEY"] = anthropic_key
            if role_base_url:
                secrets["ANTHROPIC_BASE_URL"] = str(role_base_url)
        elif backend in {"opencode", "junie", "cursor"}:
            # These backends can use either Anthropic or OpenAI providers;
            # inject whichever keys are available so the subprocess can choose.
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                val = os.environ.get(key, "")
                if val:
                    secrets[key] = val

        # Stash card_context as metadata so the performer has full access to
        # role/persona/relay_feedback/etc. without us forking the schema here.
        metadata = json.loads(json.dumps(dict(card_context), default=str))

        env_cache_path = card_context.get("env_cache_path")
        return JobInitPayload.model_validate(
            {
                "job_id": str(uuid.uuid4()),
                "card_id": str(card_context.get("id", "")),
                "role": str(card_context.get("role", "")),
                "backend": str(card_context.get("backend", "")),
                "persona": str(card_context.get("persona_instructions", "")),
                "repo_url": repo_url,
                "branch": branch,
                "secrets": secrets,
                "metadata": metadata,
                "env_cache_path": env_cache_path if env_cache_path else None,
            }
        )

    def _build_env_bootstrap_payload(
        self, card_context: dict[str, Any]
    ) -> JobInitPayload:
        """Synthesize a JobInitPayload for an env_bootstrap dispatch.

        BootstrapJobPayload is coordinare-internal and does not carry the
        repo_url/branch/secrets that JobInitPayload requires, so we derive
        them here: the repo is the symphony's own GitHub repo (the agent
        clones it to read env_spec_files), branch defaults to 'main', the
        GitHub token comes from the coordinare's environment.
        """
        import os

        from coordinare.models.performer_endpoint import JobInitPayload

        symphony_org = str(card_context.get("symphony_org") or "")
        symphony_repo = str(card_context.get("symphony_repo") or "")
        if not symphony_org or not symphony_repo:
            raise ValueError(
                "env_bootstrap card_context missing symphony_org or symphony_repo"
            )

        repo_url = f"https://github.com/{symphony_org}/{symphony_repo}.git"
        # Synthetic branch — bootstrap never pushes/PRs. clone_repository fetches
        # ``origin {branch}:{branch}`` and, if the ref doesn't exist remotely,
        # falls back to ``checkout -b`` from the shallow-cloned default branch.
        # Using a real branch (e.g. "main") triggers git's "refusing to fetch
        # into current branch" error, which surfaces as WorkspaceSetupError.
        branch = f"env-bootstrap-{uuid.uuid4().hex[:8]}"

        cache_mount_path = str(card_context.get("cache_mount_path") or "")
        env_spec_files = list(card_context.get("env_spec_files") or [])
        env_spec_contents: dict[str, str] = dict(
            card_context.get("env_spec_contents") or {}
        )

        secrets: dict[str, str] = {}
        # Prefer the daemon-injected token (fresh App installation token or
        # configured PAT, sourced via WorkspaceManager.get_fresh_github_token);
        # fall back to the coordinare process env only when not provided.
        gh_token = str(card_context.get("_github_token") or "") or os.environ.get(
            "GITHUB_TOKEN", ""
        )
        if gh_token:
            secrets["GITHUB_TOKEN"] = gh_token
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            val = os.environ.get(key, "")
            if val:
                secrets[key] = val

        spec_block = "\n\n".join(
            f"=== {path} ===\n{content}"
            for path, content in env_spec_contents.items()
        )
        persona = (
            "You are an environment bootstrap agent. Your job is to install "
            f"all build/test/runtime dependencies for this project into "
            f"the directory {cache_mount_path!r}, which is a writable volume "
            "shared with later performer containers as a read-only devenv root.\n\n"
            "Read the install/setup instructions from the following project "
            "spec files and execute the commands they describe. Use "
            f"{cache_mount_path} (NOT the repo root and NOT $HOME) as the "
            "install prefix for language toolchains, virtualenvs, node_modules, "
            "and any other generated artefacts that should persist across "
            "performer runs. Do NOT modify the cloned repo working tree, do "
            "NOT push commits, and do NOT open a PR — this job exists only to "
            "populate the cache directory.\n\n"
            "MANDATORY: After installing, write a sourceable shell script at "
            f"{cache_mount_path}/activate.sh that consumer agents will source "
            "before running their tools. It MUST export PATH (prepending any "
            "bin directories you created, e.g. virtualenv bin, node_modules/.bin, "
            "language toolchain bin), and any other env vars needed to use the "
            "installed tooling (VIRTUAL_ENV, NODE_PATH, etc.). Without this "
            "file the cache is unusable and downstream performers will reinstall. "
            "Make it idempotent and safe to source repeatedly.\n\n"
            f"Spec files ({', '.join(env_spec_files) or 'none'}):\n\n"
            f"{spec_block}\n"
        )

        meta_src = {k: v for k, v in card_context.items() if k != "_github_token"}
        # Score (performer-side dispatch payload model) requires a `title`
        # field; BootstrapJobPayload doesn't carry one, so synthesize it from
        # the symphony name. Also seed `description` from the spec block so the
        # backend agent has user-facing context if it inspects the Score.
        symphony_name = str(card_context.get("symphony_name") or "")
        meta_src.setdefault(
            "title", f"env_bootstrap: {symphony_name}" if symphony_name else "env_bootstrap"
        )
        meta_src.setdefault(
            "description",
            f"Install dev environment into {cache_mount_path} per project spec files.",
        )
        metadata = json.loads(json.dumps(meta_src, default=str))

        return JobInitPayload.model_validate(
            {
                "job_id": str(uuid.uuid4()),
                "card_id": f"env_bootstrap:{card_context.get('symphony_name', '')}",
                "role": "env_bootstrap",
                "backend": str(card_context.get("backend", "") or "claude_code"),
                "persona": persona,
                "repo_url": repo_url,
                "branch": branch,
                "secrets": secrets,
                "metadata": metadata,
                # The bootstrap *writes into* the cache mount, so the
                # container-side env_cache_path is the same directory the
                # performer will populate. Without this, service inference
                # short-circuits with skipped_reason=no_env_cache_path.
                "env_cache_path": cache_mount_path or None,
            }
        )


__all__ = ["HTTPPerformerService"]
