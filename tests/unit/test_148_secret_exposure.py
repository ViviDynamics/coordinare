"""148: no secret reaches a dashboard viewer, whichever form it takes.

Each command is taken through the performer (the tool summary and the event it
emits), then through the daemon's merge of the backend's events into state, and
then into the dashboard snapshot that every SSE subscriber receives. The secret
must be absent from the snapshot and from the activity feed, not only from one
of the stores.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from performer import models as performer_models
from performer.backends._tool_summary import MAX_TOOL_ARG, primary_argument
from performer.models import BackendEvent, BackendEventType

from coordinare.dashboard import DashboardStore
from coordinare.graph.nodes.monitor.body import _BodyCtx, _phase_merge_events
from coordinare.lib import redaction as daemon_redaction
from tests.unit.test_dashboard import _make_mock_daemon, _make_mock_health, _make_mock_metrics

_CARD = {"id": "PVTI_148", "issue_number": 148, "title": "Activity detail"}

# (the command as the agent ran it, the secret value that must never be shown)
_SECRET_COMMANDS = [
    ("curl -u user:hunter2sekret https://api.example.com", "hunter2sekret"),
    ("docker login -u me -p dockerPw77 registry.example.com", "dockerPw77"),
    ("mysql -pVALUE123 -h db.internal app", "VALUE123"),
    ("psql postgresql://u:pgSecret99@db.internal/app -c 'select 1'", "pgSecret99"),
    ("git clone https://x-access-token:plainsecret1@github.com/o/r.git", "plainsecret1"),
    ("GITHUB_TOKEN=abc123short gh pr list", "abc123short"),
    ("sshpass -p sshPass42 ssh host uptime", "sshPass42"),
    ("aws configure set aws_secret_access_key S3CRETVALUE", "S3CRETVALUE"),
    ("gh secret set DEPLOY_KEY --body DeployVal55", "DeployVal55"),
    ("curl -H 'X-API-Key: k3yvalue9' https://x.example.com", "k3yvalue9"),
    ('curl -H "Token: tok3nvalue" https://x.example.com', "tok3nvalue"),
    ('curl -H "Authorization: Bearer shorttok7" https://x.example.com', "shorttok7"),
    ("curl --user admin:adminPw1 https://x.example.com", "adminPw1"),
    ("deploy --password hunter3 --region eu", "hunter3"),
]


def _ctx(event_dict: dict[str, Any]) -> _BodyCtx:
    ctx = _BodyCtx()
    ctx.status = {"events": [event_dict]}
    ctx.card_id = _CARD["id"]
    ctx.stage = "implementing"
    return ctx


def _feed_through_daemon(store: DashboardStore, event: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Merge one performer event into daemon state, as a poll does, and return the state."""
    daemon = _make_mock_daemon(phase="monitoring_performer")
    state = daemon.state
    state.update(
        performer_events=[],
        activity_log=store.activity_log,
        current_card=dict(_CARD),
        agent_dispatch={"session_id": "sess-148", "performer_id": "implementer"},
    )
    asyncio.run(_phase_merge_events(state, _ctx(event)))
    return daemon, state


def _payload_text(store: DashboardStore, daemon: Any) -> str:
    snapshot = store.build_snapshot(daemon, _make_mock_metrics(), _make_mock_health())
    return json.dumps({"snapshot": snapshot, "feed": store.activity_log.snapshot()}, default=str)


@pytest.mark.parametrize(("command", "secret"), _SECRET_COMMANDS)
def test_a_secret_in_a_command_never_reaches_the_dashboard_payloads(command: str, secret: str) -> None:
    # The performer summarises the call and emits its tool event.
    arg = primary_argument("Bash", {"command": command})
    event = BackendEvent(type=BackendEventType.tool_use, text=f"Bash {command}", detail="shell",
                         tool="Bash", arg=arg).model_dump()
    store = DashboardStore()
    daemon, state = _feed_through_daemon(store, event)
    payload = _payload_text(store, daemon)
    assert secret not in payload, f"{secret!r} leaked through {command!r}"
    assert "[REDACTED]" in payload
    # The stored event itself is redacted, so nothing downstream can recover it.
    assert all(secret not in json.dumps(stored, default=str) for stored in state["performer_events"])


def test_a_secret_on_a_codex_command_text_is_redacted_before_it_is_stored() -> None:
    """Codex sent the raw command as the event text before this change."""
    event = {"type": "tool_use", "text": "curl -u user:codexPw8 https://x", "detail": "shell",
             "tool": "shell", "arg": "", "status": "ok", "timestamp": "2026-10-14T09:00:00+00:00"}
    store = DashboardStore()
    daemon, _ = _feed_through_daemon(store, event)
    assert "codexPw8" not in _payload_text(store, daemon)


def test_a_result_text_with_a_secret_is_redacted_in_the_payloads() -> None:
    event = {"type": "tool_use", "text": "result: GITHUB_TOKEN=resultLeak3 printed", "status": "ok",
             "timestamp": "2026-10-14T09:00:01+00:00"}
    store = DashboardStore()
    daemon, _ = _feed_through_daemon(store, event)
    assert "resultLeak3" not in _payload_text(store, daemon)


def test_a_secret_straddling_the_160_character_cap_leaves_no_prefix() -> None:
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    command = "x" * (MAX_TOOL_ARG - 19) + " " + token
    arg = primary_argument("Bash", {"command": command})
    assert len(arg) <= MAX_TOOL_ARG
    assert "A1b2C3" not in arg and "ghp_" not in arg
    store = DashboardStore()
    event = BackendEvent(type=BackendEventType.tool_use, text="Bash", tool="Bash", arg=arg).model_dump()
    daemon, _ = _feed_through_daemon(store, event)
    payload = _payload_text(store, daemon)
    assert "A1b2C3" not in payload


def test_the_performer_redacts_on_construction_too() -> None:
    event = BackendEvent(type=BackendEventType.tool_use, text="curl -u user:constructPw", detail="",
                         arg="psql postgresql://u:constructPw2@h/db")
    assert "constructPw" not in event.text
    assert "constructPw2" not in event.arg


def test_a_bare_literal_is_a_documented_limit_of_pattern_redaction() -> None:
    """No marker, no pattern: a literal with nothing around it is not recognised."""
    assert daemon_redaction.redact_command_text("echo s3cr3tvalue123") == "echo s3cr3tvalue123"


def test_the_performer_and_daemon_carry_the_same_command_patterns() -> None:
    """The two lists are copies. This keeps them from drifting apart."""
    daemon_patterns = [p.pattern for p in daemon_redaction._COMMAND_SECRET_PATTERNS]
    performer_patterns = [p.pattern for p in performer_models._COMMAND_SECRET_PATTERNS]
    assert daemon_patterns == performer_patterns


@pytest.mark.parametrize(("command", "secret"), _SECRET_COMMANDS)
def test_both_redactors_remove_every_sample(command: str, secret: str) -> None:
    assert secret not in daemon_redaction.redact_command_text(command)
    assert secret not in performer_models.redact_command(command)
