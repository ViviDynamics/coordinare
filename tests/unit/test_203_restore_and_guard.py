"""203 review round 3: nothing read from disk or restored from it reaches a viewer raw.

The guard's remaining forms, the allow-list key, tuple and set redaction, the repair audit
and the refused retune values are covered here too. Each test seeds secrets through the
path the daemon uses and then walks what a viewer would receive.
"""
from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from coordinare.daemon import CoordinareDaemon, _restored_session_dict, _synthesize_v1_session
from coordinare.graph.nodes.monitor import repair
from coordinare.graph.nodes.monitor.body import _apply_observer_retune, _BodyCtx
from coordinare.lib.redaction import redact_state_value
from coordinare.mcp.client import ToolResult
from coordinare.mcp.tools.state_tools import get_state_handler
from coordinare.state_store import PersistedSession, StateStore, WorkflowSnapshot
from tests.unit.test_203_dashboard_redaction import (
    _DYNAMIC_WRITE_ALLOWANCES,
    _SAVE_PATH_COPIES,
    BEARER,
    CURL_PASSWORD,
    DB_PASSWORD,
    ENV_TOKEN,
    SECRET_TEXTS,
    SECRET_VALUES,
    _endpoint_texts,
    _unwrapped_writers,
)

# --- B2: the two guard forms the round-2 guard missed ----------------------------------- #


@pytest.mark.parametrize(
    ("snippet", "label"),
    [
        ("self._state[k] = raw\n", "self._state[k]"),
        ("state.setdefault(k, raw)\n", "state.setdefault(k)"),
    ],
)
def test_the_guard_flags_attribute_state_and_variable_key_setdefault(snippet: str, label: str) -> None:
    assert [key for _, key in _unwrapped_writers(ast.parse(snippet))] == [label]


@pytest.mark.parametrize(
    "snippet",
    [
        "self._state[k] = redact_state_value(raw)\n",
        "state.setdefault(k, redact_state_value(raw))\n",
        "counter.setdefault(k, raw)\n",
        "self._cache[k] = raw\n",
    ],
)
def test_the_guard_passes_wrapped_and_unrelated_dynamic_forms(snippet: str) -> None:
    assert _unwrapped_writers(ast.parse(snippet)) == []


# --- F1: an allow-list entry names its function ----------------------------------------- #


def test_every_dynamic_allowance_is_keyed_by_file_function_and_expression() -> None:
    for key in _DYNAMIC_WRITE_ALLOWANCES:
        assert key.count("::") == 2, f"allow-list key lacks a function name: {key}"


def test_every_save_path_copy_is_keyed_by_file_function_and_field() -> None:
    for key in _SAVE_PATH_COPIES:
        assert key.count("::") == 2, f"save-path key lacks a function name: {key}"


# --- F2: containers other than lists and dicts ------------------------------------------ #


def test_the_helper_redacts_inside_tuples_sets_and_dict_keys() -> None:
    value = {
        f"key {SECRET_TEXTS['export']}": (SECRET_TEXTS["curl"], 2),
        "set": frozenset({SECRET_TEXTS["bearer"]}),
    }
    out = redact_state_value(value)
    text = repr(out)
    for secret in (ENV_TOKEN, CURL_PASSWORD, BEARER):
        assert secret not in text, f"a secret survived the helper: {secret[:6]}..."
    assert isinstance(out["set"], frozenset)
    (tuple_value,) = [v for k, v in out.items() if str(k).startswith("key")]
    assert isinstance(tuple_value, tuple)


# --- F3: the repair audit record ---------------------------------------------------------- #


def test_the_repair_audit_record_redacts_the_reviewer_detail() -> None:
    record = repair._repair_record(
        head_sha="abc123",
        attempt=1,
        kind="reviewer",
        now_iso="2026-10-14T00:00:00+00:00",
        is_safe=False,
        detail=SECRET_TEXTS["export"],
    )
    assert ENV_TOKEN not in json.dumps(record)


# --- F5: refused retune values -------------------------------------------------------------- #


def test_refused_retune_values_are_redacted_in_the_audit_and_the_log() -> None:
    state: dict[str, Any] = {"current_symphony": "sym", "observer_retune_audit": []}
    ctx = _BodyCtx(card_id="PVTI_203", stage="implementing")
    verdict = SimpleNamespace(retune={"poll": SECRET_TEXTS["export"]})
    observer_cfg = SimpleNamespace(retune_bounds={"poll": {"min": 1, "max": 10}})
    with capture_logs() as logs:
        _apply_observer_retune(state, ctx, verdict, observer_cfg)
    assert state["observer_retune_audit"], "the refused value produced no audit entry"
    assert ENV_TOKEN not in json.dumps(state["observer_retune_audit"], default=str)
    assert ENV_TOKEN not in json.dumps(logs, default=str)


# --- B1: restored snapshots ------------------------------------------------------------------ #


def _pre_203_snapshot_file(path: Path) -> None:
    """Write a snapshot the way a pre-203 daemon did: raw text in every guarded field."""
    session = PersistedSession(
        card_id="PVTI_203",
        open_questions=[f"Set GITHUB_TOKEN={ENV_TOKEN}?"],
        system_error_reason=f"performer failed: {SECRET_TEXTS['dburl']}",
        card_clarifications=[{"questions": [f"Use {SECRET_TEXTS['bearer']}?"], "answer": "yes"}],
    )
    snapshot = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC),
        phase="blocked",
        active_card_id="PVTI_203",
        open_questions=[f"Set GITHUB_TOKEN={ENV_TOKEN}?", SECRET_TEXTS["curl"]],
        card_clarifications=[{"questions": [f"Use {SECRET_TEXTS['bearer']}?"], "answer": "yes"}],
        active_sessions={"PVTI_203": session},
    )
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    raw = path.read_text(encoding="utf-8")
    for secret in (ENV_TOKEN, CURL_PASSWORD, DB_PASSWORD, BEARER):
        assert secret in raw, "the fixture did not write the secret to disk"


def _mcp_state_text(snapshot_json: str) -> str:
    """What the MCP state tool returns when the dashboard answers /api/state with ``snapshot_json``."""

    class _Client:
        read_budget = 1.0

        async def get_json(self, path: str, *, timeout_seconds: float) -> ToolResult:
            assert path == "/api/state"
            return ToolResult(ok=True, data=json.loads(snapshot_json))

    client: Any = _Client()
    result = asyncio.run(get_state_handler(client))
    return json.dumps(result.model_dump(exclude_none=True))


def test_a_pre_203_snapshot_serves_and_restores_no_raw_text(tmp_path: Path) -> None:
    path = tmp_path / "coordinare.state.json"
    _pre_203_snapshot_file(path)
    store = StateStore(path, MagicMock())
    loaded = asyncio.run(store.load())
    assert loaded is not None
    assert store.last_snapshot is loaded, "the dashboard reads last_snapshot"

    persisted = loaded.active_sessions["PVTI_203"]
    session = _restored_session_dict("PVTI_203", persisted, loaded, None)
    fake = SimpleNamespace(_state={"phase": "blocked", "active_sessions": {"PVTI_203": session}, "config": None})
    restore: Callable[[Any, WorkflowSnapshot], None] = CoordinareDaemon._restore_from_snapshot
    restore(fake, loaded)

    daemon = MagicMock()
    daemon.state = fake._state
    daemon.state["startup_complete"] = True
    daemon.state_store = store
    daemon._cycle_active = False
    daemon.running = True

    stored = json.dumps([
        store.last_snapshot.model_dump(mode="json"),
        session,
        fake._state,
    ], default=str)
    for secret in SECRET_VALUES:
        assert secret not in stored, f"restored state holds a raw secret: {secret[:6]}..."

    texts = _endpoint_texts({}, daemon=daemon)
    for route, text in texts.items():
        for secret in SECRET_VALUES:
            assert secret not in text, f"a restored secret reached {route}: {secret[:6]}..."
    assert "[REDACTED" in texts["/api/state"]

    mcp_text = _mcp_state_text(texts["/api/state"])
    for secret in SECRET_VALUES:
        assert secret not in mcp_text, f"a restored secret reached the MCP state tool: {secret[:6]}..."


def test_a_restarted_daemon_with_no_sessions_serves_no_raw_snapshot_text(tmp_path: Path) -> None:
    """The dashboard's fallback reads the snapshot's questions when no live session exists."""
    path = tmp_path / "coordinare.state.json"
    _pre_203_snapshot_file(path)
    store = StateStore(path, MagicMock())
    loaded = asyncio.run(store.load())
    assert loaded is not None
    daemon = MagicMock()
    daemon.state = {"phase": "blocked", "active_sessions": {}, "current_card": None}
    daemon.state_store = store
    daemon._cycle_active = False
    daemon.running = True
    texts = _endpoint_texts({}, daemon=daemon)
    for route, text in texts.items():
        for secret in SECRET_VALUES:
            assert secret not in text, f"a snapshot secret reached {route} with no sessions: {secret[:6]}..."


def test_the_v1_synthesis_of_a_pre_203_snapshot_holds_no_raw_text(tmp_path: Path) -> None:
    """The top-level copy a v1 snapshot is restored through (daemon.py _synthesize_v1_session)."""
    path = tmp_path / "coordinare.state.json"
    _pre_203_snapshot_file(path)
    loaded = asyncio.run(StateStore(path, MagicMock()).load())
    assert loaded is not None
    synthesized = _synthesize_v1_session(loaded, None)
    stored = json.dumps(synthesized, default=str)
    for secret in SECRET_VALUES:
        assert secret not in stored, f"a v1-synthesised session holds a raw secret: {secret[:6]}..."
