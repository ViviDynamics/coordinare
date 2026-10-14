"""System health detail (151): the facts behind the dashboard's System health card.

Each signal is recorded where its event already happens: a GraphQL response, a
GraphQL call that failed, a board read, a webhook delivery, a performer start,
a performer turn ending. Nothing here polls, and nothing calls an API just to
feed the card. ``build_system_health`` turns the recorded facts, the config and
the local disk into one row per subsystem.

Only exception class names and HTTP header values are recorded. Messages can
carry credentials, so they never enter this module.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from coordinare.lib.redaction import redact_state_value

__all__ = [
    "BOARD_FAILURE_STREAK_DOWN",
    "BOARD_STALE_POLL_INTERVALS",
    "DEGRADED",
    "DISK_FREE_DEGRADED_FRACTION",
    "DISK_FREE_DOWN_FRACTION",
    "DOWN",
    "FAILURE_AUTH",
    "FAILURE_CIRCUIT",
    "FAILURE_OTHER",
    "FAILURE_TRANSIENT",
    "GITHUB_FAILURE_STREAK_DOWN",
    "GITHUB_RATE_LOW_FRACTION",
    "HEALTHY",
    "MODEL_ERROR_RATE_DEGRADED",
    "MODEL_ERROR_STREAK_DEGRADED",
    "MODEL_ERROR_STREAK_DOWN",
    "MODEL_MIN_SAMPLE_TURNS",
    "MODEL_WINDOW_TURNS",
    "NOT_MONITORED",
    "PERFORMER_FAILURE_STREAK_DOWN",
    "PERFORMER_NO_ACTIVITY_FALLBACK_SECONDS",
    "PERFORMER_RUNNING_PHASES",
    "PERFORMER_START_FRESH_SECONDS",
    "STALE",
    "SYSTEM_HEALTH",
    "UNKNOWN",
    "WEBHOOK_REJECTIONS_DEGRADED",
    "WEBHOOK_REJECTION_RECORD_MAX",
    "WEBHOOK_REJECTION_WINDOW_SECONDS",
    "SystemHealthInputs",
    "SystemHealthRecorder",
    "build_system_health",
]

HEALTHY = "healthy"
DEGRADED = "degraded"
DOWN = "down"
UNKNOWN = "unknown"
STALE = "stale"
NOT_MONITORED = "not_monitored"

# How a failed call is classified when it is recorded. Only transient failures count
# toward a streak; an auth failure or an open circuit is down at once; any other error
# is recorded by class name and leaves the status alone.
FAILURE_TRANSIENT = "transient"
FAILURE_AUTH = "auth"
FAILURE_CIRCUIT = "circuit"
FAILURE_OTHER = "other"

# Named thresholds. Where a number can come from config it does (the board read
# is stale after N poll intervals); the rest are the defaults this card is judged by.
GITHUB_RATE_LOW_FRACTION = 0.10
GITHUB_FAILURE_STREAK_DOWN = 5
BOARD_FAILURE_STREAK_DOWN = 3
BOARD_STALE_POLL_INTERVALS = 3
PERFORMER_FAILURE_STREAK_DOWN = 3
PERFORMER_START_FRESH_SECONDS = 3600
MODEL_WINDOW_TURNS = 20
MODEL_MIN_SAMPLE_TURNS = 5
MODEL_ERROR_RATE_DEGRADED = 0.25
MODEL_ERROR_STREAK_DEGRADED = 2
MODEL_ERROR_STREAK_DOWN = 3
DISK_FREE_DEGRADED_FRACTION = 0.10
DISK_FREE_DOWN_FRACTION = 0.02
WEBHOOK_REJECTIONS_DEGRADED = 5
WEBHOOK_REJECTION_WINDOW_SECONDS = 3600
WEBHOOK_REJECTION_RECORD_MAX = 100
# The phases in which coordinare is watching a live performer job. A card waiting on CI or review
# (monitoring_pr, relay_feedback) has no performer running, so it is not counted in flight.
PERFORMER_RUNNING_PHASES: tuple[str, ...] = ("dispatching", "monitoring_agent", "monitoring_performer")
# With no stall or role timeout configured, a running performer with no activity this long is degraded.
PERFORMER_NO_ACTIVITY_FALLBACK_SECONDS = 7200

_SEVERITY: dict[str, int] = {HEALTHY: 0, UNKNOWN: 1, STALE: 1, DEGRADED: 2, DOWN: 3}
_RUNTIME_NAMES: dict[str, str] = {"DockerRuntime": "Docker", "KubernetesRuntime": "Kubernetes"}
_MODEL_CAVEAT = "system errors also cover container and transport failures"
_NAMES: dict[str, str] = {
    "github_api": "GitHub API",
    "github_webhooks": "GitHub webhooks",
    "board_check": "Board check",
    "performer_runtime": "Performer runtime",
    "model_backends": "Model backends",
    "disk_env_cache": "Disk and env cache",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Track:
    """One signal's attempts: when it was last seen, when it last worked, and the failure streak."""

    last_at: datetime | None = None
    last_success_at: datetime | None = None
    streak: int = 0
    last_error: str | None = None
    last_kind: str | None = None

    def record(self, *, ok: bool, at: datetime, error: str | None, kind: str = FAILURE_TRANSIENT) -> None:
        self.last_at = at
        if ok:
            self.last_success_at = at
            self.streak = 0
            self.last_error = None
            self.last_kind = None
            return
        self.last_error = redact_state_value(error or "error")
        if kind == FAILURE_OTHER:
            return
        self.last_kind = kind
        self.streak += 1


@dataclass
class Rate:
    """The GitHub GraphQL quota as the last response reported it."""

    remaining: int | None = None
    limit: int | None = None
    reset_at: datetime | None = None
    read_at: datetime | None = None


class SystemHealthRecorder:
    """Process-wide record of what the daemon has seen. Written by the event sites, read at snapshot time."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.github = Track()
        self.rate = Rate()
        self.webhook_accepted_at: datetime | None = None
        self.webhook_rejections: deque[datetime] = deque(maxlen=WEBHOOK_REJECTION_RECORD_MAX)
        self.boards: dict[str, Track] = {}
        self.performer = Track()
        self.runtime: str | None = None
        self.model = Track()
        self.model_window: deque[bool] = deque(maxlen=MODEL_WINDOW_TURNS)

    def record_github_rate(self, headers: object, *, at: datetime | None = None) -> None:
        """Read the quota headers of one GraphQL response (or a rate-limited one)."""
        if not isinstance(headers, Mapping):
            return
        lowered = {str(key).lower(): str(value) for key, value in headers.items()}
        remaining = _int_or_none(lowered.get("x-ratelimit-remaining"))
        if remaining is None:
            return
        self.rate = Rate(
            remaining=remaining,
            limit=_int_or_none(lowered.get("x-ratelimit-limit")),
            reset_at=_epoch_or_none(lowered.get("x-ratelimit-reset")),
            read_at=at or _utc_now(),
        )

    def record_github_call(
        self, *, ok: bool, error: str | None = None, kind: str = FAILURE_TRANSIENT, at: datetime | None = None,
    ) -> None:
        self.github.record(ok=ok, at=at or _utc_now(), error=error, kind=kind)

    def record_webhook(self, *, accepted: bool, at: datetime | None = None) -> None:
        stamp = at or _utc_now()
        if accepted:
            self.webhook_accepted_at = stamp
        else:
            self.webhook_rejections.append(stamp)

    def record_board_poll(self, board: str, *, ok: bool, error: str | None = None, at: datetime | None = None) -> None:
        self.boards.setdefault(board, Track()).record(ok=ok, at=at or _utc_now(), error=error)

    def record_performer_start(
        self, runtime: str, *, ok: bool, error: str | None = None, at: datetime | None = None,
    ) -> None:
        self.runtime = runtime
        self.performer.record(ok=ok, at=at or _utc_now(), error=error)

    def record_model_turn(self, *, ok: bool, at: datetime | None = None) -> None:
        self.model.record(ok=ok, at=at or _utc_now(), error="system_error")
        self.model_window.append(ok)


SYSTEM_HEALTH = SystemHealthRecorder()


@dataclass(frozen=True)
class SystemHealthInputs:
    """Everything the card reads from outside the recorder, gathered at snapshot time."""

    now: datetime
    health: Mapping[str, str]
    poll_interval_seconds: int | None
    webhooks_enabled: bool
    performer_modes: tuple[str, ...]
    active_performers: int
    disk_free_fraction: float | None
    env_symphonies: int
    env_failed: tuple[str, ...]
    env_ready: int
    # The oldest running performer's dispatch time, the newest activity on a running card,
    # and the threshold past which a quiet running performer is hung (role timeout, else stall timeout).
    running_since: datetime | None = None
    last_activity_at: datetime | None = None
    no_activity_limit_seconds: int = PERFORMER_NO_ACTIVITY_FALLBACK_SECONDS


def _int_or_none(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _epoch_or_none(raw: str | None) -> datetime | None:
    value = _int_or_none(raw)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _newest(*stamps: datetime | None) -> datetime | None:
    present = [stamp for stamp in stamps if stamp is not None]
    return max(present) if present else None


class _Fresh(NamedTuple):
    """How a row says how fresh it is: the label that leads its freshness text, and the times."""

    label: str
    checked_at: datetime | None
    last_success_at: datetime | None = None


def _row(key: str, status: str, summary: str, fresh: _Fresh, reason: str | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "name": _NAMES[key],
        "status": status,
        "summary": summary,
        "reason": reason,
        "fresh_label": fresh.label,
        "checked_at": _iso(fresh.checked_at),
        "last_success_at": _iso(fresh.last_success_at),
    }


def _worst(findings: list[tuple[str, str]]) -> tuple[str, str | None]:
    """The most severe status among the findings, and the reasons that reached it."""
    if not findings:
        return HEALTHY, None
    top = max((status for status, _ in findings), key=_SEVERITY.__getitem__)
    return top, "; ".join(reason for status, reason in findings if status == top)


def _count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular}" if n == 1 else f"{n} {plural or singular + 's'}"


def _github_findings(rec: SystemHealthRecorder, inputs: SystemHealthInputs) -> list[tuple[str, str]]:
    """What the failure streak, the quota and the daemon's own probe say about the GitHub API."""
    track, rate = rec.github, rec.rate
    findings: list[tuple[str, str]] = []
    if track.streak and track.last_kind == FAILURE_AUTH:
        findings.append((DOWN, "GitHub rejected the token"))
    elif track.streak and track.last_kind == FAILURE_CIRCUIT:
        findings.append((DOWN, "circuit open"))
    elif track.streak >= GITHUB_FAILURE_STREAK_DOWN:
        findings.append((DOWN, f"last {track.streak} calls failed ({track.last_error})"))
    elif track.streak:
        findings.append((DEGRADED, f"last call failed ({track.last_error})"))

    reset_passed = rate.reset_at is not None and rate.reset_at <= inputs.now
    if rate.remaining is not None and reset_passed:
        findings.append((STALE, "rate reading is stale: its reset time has passed"))
    elif rate.remaining == 0 and rate.reset_at is not None:
        findings.append((DOWN, "rate limit exhausted until it resets"))
    elif rate.remaining is not None and rate.limit and rate.remaining < rate.limit * GITHUB_RATE_LOW_FRACTION:
        findings.append((DEGRADED, "rate limit nearly used up"))
    if inputs.health.get("github") == DEGRADED:
        findings.append((DEGRADED, "the daemon's GitHub probe is degraded"))
    return findings


def _quota_summary(rate: Rate) -> str:
    if rate.remaining is None:
        return "no quota reading yet; GraphQL responses carry it"
    if not rate.limit:
        return f"{rate.remaining:,} requests left"
    summary = f"{rate.remaining:,} of {rate.limit:,} requests left"
    if rate.reset_at is not None:
        summary += f", resets {rate.reset_at.strftime('%H:%M UTC')}"
    return summary


def _github_row(rec: SystemHealthRecorder, inputs: SystemHealthInputs) -> dict[str, Any]:
    track, rate = rec.github, rec.rate
    summary = _quota_summary(rate)
    status, reason = _worst(_github_findings(rec, inputs))
    if status == HEALTHY and track.last_at is None and rate.read_at is None:
        status, reason = UNKNOWN, "nothing observed since this daemon started"
    fresh = _Fresh("last call", _newest(track.last_at, rate.read_at), track.last_success_at)
    return _row("github_api", status, summary, fresh, reason)


def _webhook_row(rec: SystemHealthRecorder, inputs: SystemHealthInputs) -> dict[str, Any]:
    accepted = rec.webhook_accepted_at
    if not inputs.webhooks_enabled:
        return _row(
            "github_webhooks", NOT_MONITORED, "not in use", _Fresh("last delivery", None),
            "coordinare polls the board on a timer (webhooks.enabled is false), "
            "so GitHub changes are seen on the next check",
        )
    window_start = inputs.now - timedelta(seconds=WEBHOOK_REJECTION_WINDOW_SECONDS)
    recent = [stamp for stamp in rec.webhook_rejections if stamp >= window_start]
    newest_rejection = max(recent) if recent else None
    note = (
        f"; {len(recent)} rejected in the last {WEBHOOK_REJECTION_WINDOW_SECONDS // 60} min" if recent else ""
    )
    fresh = _Fresh("last delivery", _newest(accepted, newest_rejection), accepted)
    if (
        newest_rejection is not None
        and len(recent) >= WEBHOOK_REJECTIONS_DEGRADED
        and (accepted is None or accepted < newest_rejection)
    ):
        return _row(
            "github_webhooks", DEGRADED, "deliveries are failing the signature check" + note, fresh,
            f"{len(recent)} bad signatures in the window and no valid delivery since them",
        )
    if accepted is not None:
        return _row("github_webhooks", HEALTHY, "deliveries are arriving" + note, fresh)
    if recent:
        return _row("github_webhooks", UNKNOWN, "no valid delivery since this daemon started" + note, fresh)
    if rec.webhook_rejections:
        # Rejections are older than the window: say that nothing valid has arrived, not that nothing arrived.
        return _row(
            "github_webhooks", UNKNOWN, "no valid delivery since this daemon started", fresh,
            "only rejected deliveries have arrived",
        )
    return _row(
        "github_webhooks", UNKNOWN, "no delivery since this daemon started", fresh,
        "nothing observed since this daemon started",
    )


def _board_row(rec: SystemHealthRecorder, inputs: SystemHealthInputs) -> dict[str, Any]:
    if not rec.boards:
        return _row(
            "board_check", UNKNOWN, "no board check yet", _Fresh("last checked", None),
            "nothing observed since this daemon started",
        )
    poll = inputs.poll_interval_seconds
    findings: list[tuple[str, str]] = []
    worst_streak = max(track.streak for track in rec.boards.values())
    worst_error = next(
        (track.last_error for track in rec.boards.values() if track.streak == worst_streak), None,
    )
    if worst_streak >= BOARD_FAILURE_STREAK_DOWN:
        findings.append((DOWN, f"{worst_streak} board checks failed in a row ({worst_error})"))
    elif worst_streak:
        findings.append((DEGRADED, f"last board check failed ({worst_error})"))
    elif poll:
        # Staleness only means something while polling is on. With polling off a quiet repo
        # is simply quiet, so the last check time is shown neutrally and never degrades the row.
        threshold = timedelta(seconds=poll * BOARD_STALE_POLL_INTERVALS)
        for track in rec.boards.values():
            if track.last_success_at is None or inputs.now - track.last_success_at > threshold:
                findings.append((DEGRADED, f"no successful board check in the last {int(threshold.total_seconds())}s"))
                break

    status, reason = _worst(findings)
    if worst_streak:
        summary = f"{worst_streak} failed in a row"
    elif poll:
        summary = f"reads the board every {poll}s"
    else:
        summary = "reads the board on webhook events; polling is off"
    fresh = _Fresh(
        "last checked",
        _newest(*(track.last_at for track in rec.boards.values())),
        _newest(*(track.last_success_at for track in rec.boards.values())),
    )
    return _row("board_check", status, summary, fresh, reason)


def _performer_row(rec: SystemHealthRecorder, inputs: SystemHealthInputs) -> dict[str, Any]:
    modes = inputs.performer_modes
    if not modes:
        return _row(
            "performer_runtime", NOT_MONITORED, "not in use", _Fresh("last start", None),
            "no performer endpoints are configured, so performers run as local subprocesses "
            "and there is no container runtime to probe",
        )
    if "ephemeral" not in modes:
        return _row(
            "performer_runtime", NOT_MONITORED, "not in use", _Fresh("last start", None),
            "the performers are persistent endpoints that coordinare does not start, "
            "so there is no start to observe",
        )
    track = rec.performer
    in_flight = _count(inputs.active_performers, "performer") + " in flight"
    if track.last_at is None:
        return _row(
            "performer_runtime", UNKNOWN, in_flight, _Fresh("last start", None),
            "no performer started since this daemon started",
        )
    findings: list[tuple[str, str]] = []
    if track.streak >= PERFORMER_FAILURE_STREAK_DOWN:
        findings.append((DOWN, f"last {track.streak} starts failed ({track.last_error})"))
    elif track.streak:
        findings.append((DEGRADED, f"last start failed ({track.last_error})"))
    if inputs.health.get("agent") == DEGRADED:
        findings.append((DEGRADED, "the daemon's agent probe is degraded"))
    if inputs.active_performers > 0 and inputs.running_since is not None:
        quiet_since = max(inputs.running_since, inputs.last_activity_at) if inputs.last_activity_at else inputs.running_since
        quiet = inputs.now - quiet_since
        if quiet > timedelta(seconds=inputs.no_activity_limit_seconds):
            findings.append((DEGRADED, f"performer running for {int(quiet.total_seconds() // 60)} min with no activity"))
    runtime = _RUNTIME_NAMES.get(rec.runtime or "", rec.runtime or "runtime")
    summary = f"{runtime} · {in_flight}"
    status, reason = _worst(findings)
    # Nothing running now and a last success that is not recent: neutral, never green-fresh.
    idle_too_long = (
        track.last_success_at is not None
        and inputs.now - track.last_success_at > timedelta(seconds=PERFORMER_START_FRESH_SECONDS)
    )
    if status == HEALTHY and inputs.active_performers == 0 and idle_too_long:
        status = STALE
        reason = "no performer is running, and the last start is older than the freshness threshold"
    fresh = _Fresh("last start", track.last_at, track.last_success_at)
    return _row("performer_runtime", status, summary, fresh, reason)


def _model_row(rec: SystemHealthRecorder, inputs: SystemHealthInputs) -> dict[str, Any]:
    track = rec.model
    window = list(rec.model_window)
    total = len(window)
    failed = sum(1 for ok in window if not ok)
    summary = f"{failed} of last {total} turns ended in a system error"
    if track.last_at is None:
        return _row(
            "model_backends", UNKNOWN, summary, _Fresh("last turn", None),
            "no performer turn has ended since this daemon started",
        )
    findings: list[tuple[str, str]] = []
    if track.streak >= MODEL_ERROR_STREAK_DOWN:
        findings.append((DOWN, f"last {track.streak} turns ended in a system error"))
    elif track.streak >= MODEL_ERROR_STREAK_DEGRADED:
        findings.append((DEGRADED, f"last {track.streak} turns ended in a system error"))
    # The rate is only meaningful once there is a sample; below it, only the streak rules apply.
    if total >= MODEL_MIN_SAMPLE_TURNS and failed / total >= MODEL_ERROR_RATE_DEGRADED:
        findings.append((DEGRADED, summary))
    status, reason = _worst(findings)
    if reason is not None:
        reason = f"{reason}; {_MODEL_CAVEAT}"
    fresh = _Fresh("last turn", track.last_at, track.last_success_at)
    return _row("model_backends", status, summary, fresh, reason)


def _disk_env_row(inputs: SystemHealthInputs) -> dict[str, Any]:
    free = inputs.disk_free_fraction
    findings: list[tuple[str, str]] = []
    if free is not None and free < DISK_FREE_DOWN_FRACTION:
        findings.append((DOWN, "disk is almost full"))
    elif free is not None and free < DISK_FREE_DEGRADED_FRACTION:
        findings.append((DEGRADED, "disk is running low"))
    # This row is shown to every scope, so it counts failing symphonies and never names one.
    if inputs.env_failed:
        findings.append((DEGRADED, f"env cache bootstrap failed for {_count(len(inputs.env_failed), 'symphony', 'symphonies')}"))

    summary = f"{round(free * 100)}% of disk free" if free is not None else "disk usage unknown"
    if inputs.env_symphonies == 0:
        summary += "; no env cache in use"
    elif inputs.env_ready == inputs.env_symphonies:
        summary += f"; env cache ready for {_count(inputs.env_ready, 'symphony', 'symphonies')}"
    else:
        summary += f"; env cache ready for {inputs.env_ready} of {inputs.env_symphonies}"

    status, reason = _worst(findings)
    if status == HEALTHY and free is None:
        status, reason = UNKNOWN, "disk usage could not be read"
    return _row("disk_env_cache", status, summary, _Fresh("measured", inputs.now), reason)


def _overall(rows: list[dict[str, Any]]) -> str:
    """The worst status among the rows that are monitored. Not-monitored rows never make it green."""
    statuses: list[str] = [str(row["status"]) for row in rows if row["status"] != NOT_MONITORED]
    if not statuses:
        return UNKNOWN
    return max(statuses, key=_SEVERITY.__getitem__)


def build_system_health(recorder: SystemHealthRecorder, inputs: SystemHealthInputs) -> dict[str, Any]:
    """One row per subsystem in the issue's order, and the overall status they add up to."""
    rows = [
        _github_row(recorder, inputs),
        _webhook_row(recorder, inputs),
        _board_row(recorder, inputs),
        _performer_row(recorder, inputs),
        _model_row(recorder, inputs),
        _disk_env_row(inputs),
    ]
    return {"overall": _overall(rows), "rows": rows}
