"""203: no text a dashboard viewer receives carries a secret the performer produced.

The fields that reached viewers raw (performer stderr, open questions, the local
test gate's failing output, observer verdicts, system error reasons) are now
redacted where the text enters state. These tests drive the real entry points
with secrets in every shape the redactor knows, then walk everything the
dashboard and the MCP state tool would return.
"""
from __future__ import annotations

import ast
import asyncio
import fnmatch
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.graph.nodes.monitor.body import (
    _BodyCtx,
    _phase_local_test_gate,
    _phase_review_routes_s2,
)
from coordinare.lib.redaction import (
    _TOO_DEEP_MARKER,
    MAX_STATE_DEPTH,
    TEXT_FIELD_GLOBS,
    TEXT_FIELD_NAMES,
    redact_state_value,
)
from coordinare.mcp.client import ToolResult
from coordinare.mcp.tools.state_tools import get_state_handler
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.env_cache import EnvCacheService
from coordinare.services.intake_dispatch import register_failure
from coordinare.services.observer import OBSERVER_VERDICTS, parse_verdict, record_recent_verdict
from coordinare.transport import subprocess_transport
from coordinare.transport.subprocess_transport import SubprocessTransport, _fetch_session_stats

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "coordinare"

# Each secret value is the part that must never reach a viewer.
GITHUB_PAT = "ghp_" + "Ab3" * 12
ENV_TOKEN = "supersecretvalue123"
CURL_PASSWORD = "hunter2pass"
DB_PASSWORD = "dbpass9"
BEARER = "bearerValue0123456789abcdefXYZ"

SECRET_TEXTS = {
    "ghp_token": f"push used {GITHUB_PAT} and failed",
    "export": f"export GITHUB_TOKEN={ENV_TOKEN}",
    "curl": f"curl -u deployer:{CURL_PASSWORD} https://api.example.com/health",
    "dburl": f"postgresql://dbuser:{DB_PASSWORD}@db.internal:5432/app",
    "bearer": f"Authorization: Bearer {BEARER}",
}
SECRET_VALUES = (GITHUB_PAT, ENV_TOKEN, CURL_PASSWORD, DB_PASSWORD, BEARER)

TEXT_FIELDS = TEXT_FIELD_NAMES


def _all_text() -> str:
    return " ".join(SECRET_TEXTS.values())


class _FakeProc:
    """A stderr pipe that yields the given lines, as the performer process would."""

    def __init__(self, lines: list[str]) -> None:
        self.stderr = self._lines(lines)

    @staticmethod
    async def _lines(lines: list[str]) -> Any:
        for line in lines:
            yield (line + "\n").encode()


class _FakeHttpResponse:
    """What the backend's /api/session returns for a session with a model-written title."""

    is_success = True
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {"title": SECRET_TEXTS["export"], "filesChanged": 2, "linesAdded": 3, "linesRemoved": 1}


class _FakeHttpClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args

    async def __aenter__(self) -> _FakeHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str) -> _FakeHttpResponse:
        return _FakeHttpResponse()


def _seed_performer_state() -> dict[str, Any]:
    """Run the real entry points on secret-bearing performer output.

    Returns the state the daemon would hold after these writers ran. Each field the
    review named is driven where it has a cheap entry point. The symphony ``last_error``,
    ``convergence_reason`` and ``assessor_open_questions`` writers are inline in large
    phase functions, so the static writer guard covers them instead.
    """
    state: dict[str, Any] = {"local_fix_counter": {"abc": 9}}

    # Observer: the judge's reason and an evidence string, through parse and record.
    verdict = parse_verdict({"data": {
        "verdict": sorted(OBSERVER_VERDICTS)[0],
        "reason": SECRET_TEXTS["export"],
    }})
    assert verdict is not None
    state["observer_verdicts"] = record_recent_verdict(
        None, verdict, ["stall"], {"note": SECRET_TEXTS["curl"], "count": 3}, "2026-10-14T00:00:00+00:00",
    )

    # Local test gate: the failing output escalates to the operator, and the raw copy
    # stays in the re-dispatch path only.
    ctx = _BodyCtx(
        body=f"FAILED test_db\n{SECRET_TEXTS['dburl']}\n{SECRET_TEXTS['bearer']}",
        card_id="PVTI_203",
        comments=[],
        stage="implementing",
        status={"local_test_failed": True, "head_after": "abc"},
    )
    asyncio.run(_phase_review_routes_s2(state, ctx))

    # Environment blocker: the performer's reason goes to env_blocked and the hold reason.
    env_ctx = _BodyCtx(
        card_id="PVTI_203",
        stage="implementing",
        marker="env_blocked",
        status={"reason": SECRET_TEXTS["ghp_token"]},
    )
    asyncio.run(_phase_local_test_gate(state, env_ctx))

    # Intake failure: last_<role>_error is written by register_failure.
    intake = SimpleNamespace(
        symphony_name="sym", advocate_attempts=0, last_advocate_succeeded=None, last_advocate_error=None,
        last_advocate_run_at=None, advocate_exhausted=False,
    )
    register_failure(intake, "advocate", f"dispatch failed: {SECRET_TEXTS['curl']}", max_attempts=3)
    state["intake"] = intake

    # Env bootstrap failure: last_bootstrap_error is written by on_bootstrap_complete.
    cache = EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=Path("/tmp/203-env-sym"))
    state["env_cache"] = {"sym": cache}

    async def _bootstrap_failed() -> None:
        EnvCacheService(None).on_bootstrap_complete(
            "sym", False, state, error=f"bootstrap failed: {SECRET_TEXTS['dburl']}",
        )

    asyncio.run(_bootstrap_failed())

    # Session title: model-written text from the backend's /api/session, fetched for real.
    with patch.object(subprocess_transport.httpx, "AsyncClient", _FakeHttpClient):
        state["session_stats"] = asyncio.run(_fetch_session_stats("http://127.0.0.1:4096"))

    # Clarification history: the question is copied from open_questions, as check_board does.
    state["card_clarifications"] = [{"questions": list(state.get("open_questions") or []), "answer": ""}]

    # Performer stderr: the subprocess transport's buffer, the same one the dashboard reads.
    transport = SubprocessTransport(executable="performer", timeout=5)
    proc: Any = _FakeProc([SECRET_TEXTS["export"], SECRET_TEXTS["curl"], SECRET_TEXTS["bearer"]])
    asyncio.run(transport._drain_stderr(proc))
    state["performer_logs"] = transport.agent_logs
    return state


def _daemon_for(state: dict[str, Any]) -> MagicMock:
    """A daemon whose state the dashboard reads, holding one blocked live session."""
    daemon = MagicMock()
    daemon.state = {
        "phase": "blocked",
        "current_card": {"id": "PVTI_203", "title": "Redact", "issue_number": 203},
        "slot_manager": None,
        "agent_service": SimpleNamespace(get_agent_logs=lambda: list(state["performer_logs"])),
        "active_sessions": {
            "PVTI_203": {
                "phase": "blocked",
                "performer_stage": "implementing",
                "current_card": {"title": "Redact", "issue_number": 203},
                "open_questions": list(state.get("open_questions") or []),
                "system_error_reason": state.get("system_error_reason"),
                "env_health_hold_reason": state.get("env_health_hold_reason"),
                "env_blocked": state.get("env_blocked"),
                "observer_verdicts": list(state.get("observer_verdicts") or []),
                "card_clarifications": list(state.get("card_clarifications") or []),
                "assessor_open_questions": list(state.get("assessor_open_questions") or []),
                "performer_events": [],
            },
        },
        "open_questions": list(state.get("open_questions") or []),
        "system_error_reason": state.get("system_error_reason"),
        "observer_verdicts": list(state.get("observer_verdicts") or []),
        "env_blocked": state.get("env_blocked"),
        "env_health_hold_reason": state.get("env_health_hold_reason"),
        "env_cache": state.get("env_cache") or {},
        "session_stats": state.get("session_stats"),
        "convergence_reason": state.get("convergence_reason"),
        "symphony_configs": {"sym": SimpleNamespace(
            github_project_number=1, enabled=True, overrides={}, personas={},
            env_spec_files=["README.md"], env_bootstrap_performer_id=None, github_org="acme",
        )},
        "symphony_states": {"sym": SimpleNamespace(
            cycle_count=0, error_count=0, last_error=state.get("symphony_last_error"), last_poll_at=None,
            active_card=None, board_snapshot=None, board_titles={}, board_issue_numbers={},
            board_issue_urls={}, board_pr_urls={}, session_skip_reasons={}, todo_waits={},
            todo_capacity=None, active_sessions={},
        )},
    }
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = False
    daemon.running = True
    return daemon


def _health() -> MagicMock:
    health = MagicMock()
    health.snapshot.return_value.probes = []
    return health


def _snapshot_from(state: dict[str, Any]) -> dict[str, Any]:
    """Build the dashboard snapshot for a live blocked session holding ``state``."""
    return DashboardStore().build_snapshot(_daemon_for(state), None, _health())


def _endpoint_texts(state: dict[str, Any], *, daemon: Any = None) -> dict[str, str]:
    """The response bodies of every dashboard route a viewer reads, after the same seeding."""
    store = DashboardStore()
    store.activity_log.record_many([{
        "activity_type": "performer_event", "card_id": "PVTI_203", "card_number": 203,
        "card_title": "Redact", "stage": "implementing", "text": SECRET_TEXTS["bearer"],
    }])
    daemon = daemon if daemon is not None else _daemon_for(state)
    app = create_dashboard_app(store, daemon, MagicMock(), _health())
    client = TestClient(app, base_url="http://127.0.0.1:8090")
    paths = ("/api/state", "/api/symphonies", "/api/symphonies/sym", "/api/activity")
    texts = {path: client.get(path).text for path in paths}

    async def _first_frame() -> str:
        stream = store.sse_stream(daemon, MagicMock(), _health())
        try:
            return await stream.__anext__()
        finally:
            await stream.aclose()
            store.shutdown()

    texts["sse state_update"] = asyncio.run(_first_frame())
    assert "event: state_update" in texts["sse state_update"]
    return texts


# --- the helper ---------------------------------------------------------------- #


def test_the_helper_redacts_strings_inside_lists_and_dicts() -> None:
    value = {"reason": SECRET_TEXTS["export"], "items": [SECRET_TEXTS["curl"], 3, None], "n": 2}
    out = redact_state_value(value)
    assert ENV_TOKEN not in json.dumps(out)
    assert CURL_PASSWORD not in json.dumps(out)
    assert out["n"] == 2 and out["items"][1:] == [3, None]


def test_the_helper_passes_plain_values_through() -> None:
    assert redact_state_value(None) is None
    assert redact_state_value(7) == 7
    assert redact_state_value("no secret here") == "no secret here"
    assert redact_state_value([]) == []


def test_a_cyclic_value_terminates_at_the_depth_bound() -> None:
    cyclic: dict[str, Any] = {"reason": SECRET_TEXTS["export"]}
    cyclic["self"] = cyclic
    out = redact_state_value(cyclic)
    assert ENV_TOKEN not in json.dumps(out, default=str)
    depth = 0
    node: Any = out
    while isinstance(node, dict) and "self" in node:
        node = node["self"]
        depth += 1
    assert depth == MAX_STATE_DEPTH


def test_an_adversarially_deep_list_is_cut_to_the_marker() -> None:
    deep: Any = SECRET_TEXTS["bearer"]
    for _ in range(MAX_STATE_DEPTH * 3):
        deep = [deep]
    assert _TOO_DEEP_MARKER in json.dumps(redact_state_value(deep))


def test_a_shallow_value_is_unchanged_by_the_depth_bound() -> None:
    shallow = {"a": [{"b": ["plain"]}]}
    assert redact_state_value(shallow) == shallow


# --- the entry points -------------------------------------------------------------- #


def test_the_entry_points_store_no_raw_secret() -> None:
    state = _seed_performer_state()
    stored = json.dumps(
        {key: state.get(key) for key in (*TEXT_FIELDS, "observer_verdicts", "performer_logs")},
        default=str,
    )
    for value in SECRET_VALUES:
        assert value not in stored, f"a secret reached state: {value[:6]}..."
    assert state["open_questions"], "the local test gate escalation did not run"
    assert state["env_blocked"] is not None, "the environment blocker did not run"


def test_the_re_dispatch_copy_keeps_the_raw_output_only_for_the_implementer() -> None:
    """The self-fix path hands the implementer its raw failing output, as before."""
    state: dict[str, Any] = {"local_fix_counter": {"abc": 0}}
    ctx = _BodyCtx(
        body="", card_id="PVTI_203", comments=[{"body": SECRET_TEXTS["dburl"]}],
        stage="implementing", status={"local_test_failed": True, "head_after": "abc"},
    )
    asyncio.run(_phase_review_routes_s2(state, ctx))
    assert state["relay_feedback"] == ctx.comments
    assert DB_PASSWORD in json.dumps(state["relay_feedback"])
    assert DB_PASSWORD not in json.dumps({k: state.get(k) for k in TEXT_FIELDS})


# --- the dashboard and the MCP state tool ------------------------------------------ #


def test_no_secret_appears_anywhere_in_the_dashboard_snapshot() -> None:
    state = _seed_performer_state()
    text = json.dumps(_snapshot_from(state), default=str)
    for value in SECRET_VALUES:
        assert value not in text, f"a secret reached the dashboard: {value[:6]}..."
    assert "[REDACTED]" in text, "the snapshot carried no redacted text, so the walk proved nothing"


def test_no_secret_appears_in_any_route_or_stream_a_viewer_reads() -> None:
    """The walk covers /api/state, /api/symphonies, /api/symphonies/sym, /api/activity and one SSE frame."""
    texts = _endpoint_texts(_seed_performer_state())
    for path, text in texts.items():
        for value in SECRET_VALUES:
            assert value not in text, f"a secret reached {path}: {value[:6]}..."
    assert "[REDACTED" in texts["/api/state"], "the state route carried no redacted text, so the walk proved nothing"


def test_the_seeded_intake_and_bootstrap_errors_are_stored_redacted() -> None:
    state = _seed_performer_state()
    stored = json.dumps([state["intake"].last_advocate_error, state["env_cache"]["sym"].last_bootstrap_error])
    for value in (CURL_PASSWORD, DB_PASSWORD):
        assert value not in stored
    assert "[REDACTED]" in stored


def test_no_secret_appears_in_the_mcp_state_tool_output() -> None:
    snapshot = _snapshot_from(_seed_performer_state())

    class _Client:
        read_budget = 1.0

        async def get_json(self, path: str, *, timeout_seconds: float) -> ToolResult:
            assert path == "/api/state"
            return ToolResult(ok=True, data=json.loads(json.dumps(snapshot, default=str)))

    client: Any = _Client()
    result = asyncio.run(get_state_handler(client))
    text = json.dumps(result.model_dump(exclude_none=True))
    for value in SECRET_VALUES:
        assert value not in text, f"a secret reached the MCP state tool: {value[:6]}..."



# --- every writer of a text field goes through the helper -------------------------- #

_SCANNED = ("graph", "services", "daemon.py", "cancel.py")
_LOGGER_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for part in _SCANNED:
        target = SRC / part
        files.extend(sorted(target.rglob("*.py")) if target.is_dir() else [target])
    return files


def _wrapped(value: ast.expr) -> bool:
    """True when the value is the helper's result or a value that holds no text."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        # 155: a length bound may wrap the redaction, never replace it. The bound runs after the
        # redaction, so a secret is never cut at the boundary and left half-visible.
        if value.func.id == "clip_session_error" and value.args:
            return _wrapped(value.args[0])
        return value.func.id == "redact_state_value"
    if isinstance(value, ast.Constant):
        return True
    return isinstance(value, (ast.List, ast.Dict)) and not (value.elts if isinstance(value, ast.List) else value.keys)


# Field names that can be built at run time, such as ``last_{role}_error`` in the env cache.
TEXT_GLOBS = TEXT_FIELD_GLOBS
# Dynamic writes the guard cannot resolve to a field name. Each one is a deliberate decision:
# the key ``"<file relative to src/coordinare>::<target expression>"`` maps to the reason it
# cannot carry performer text. A new dynamic write fails the guard until it is listed here.
_COPY_FROM_WRITER = (
    "copies a value a writer already produced; the writers wrap the dict literal or the field at source"
)
_SESSION_RECORD = (
    "stores a session record built by the session writers, whose text fields are redacted where they are set"
)
# Save-path copies. ``_persist_one_session`` and the snapshot builder copy live session values
# into the persisted snapshot without redacting them again. Each value was redacted by its writer
# when it entered the live state, and the restore path (``_restored_session_dict``,
# ``_restore_from_snapshot`` and the env-cache rehydrate) redacts again, so snapshots written
# before this change are still redacted on load. Keyed "<file>::<function>::<field>".
_SAVE_PATH_COPIES: dict[str, str] = {
    "daemon.py::_persist_one_session::open_questions": _COPY_FROM_WRITER,
    "daemon.py::_persist_one_session::system_error_reason": _COPY_FROM_WRITER,
    "daemon.py::_persist_one_session::env_blocked": _COPY_FROM_WRITER,
    "daemon.py::_persist_one_session::assessor_open_questions": _COPY_FROM_WRITER,
    "daemon.py::_persist_env_cache::last_bootstrap_error": _COPY_FROM_WRITER,
    "daemon.py::_persist_env_cache::last_wiki_init_error": _COPY_FROM_WRITER,
    "daemon.py::_build_snapshot::open_questions": _COPY_FROM_WRITER,
    "daemon.py::_persist_one_session::card_clarifications": _COPY_FROM_WRITER,
    "daemon.py::_build_snapshot::card_clarifications": _COPY_FROM_WRITER,
}
_DYNAMIC_WRITE_ALLOWANCES: dict[str, str] = {
    'services/intake_dispatch.py::register_failure::last_*_error': 'the error is redacted once at the top of register_failure; the setattr writes that value',
    'graph/nodes/check_board.py::check_board::_sessions_pre[_flat_id_pre]': 'stores a session record built by the session writers, whose text fields are redacted where they are set',
    'graph/nodes/check_board.py::_readopt_in_progress_cards::active_sessions[item]': 'stores a session record built by the session writers, whose text fields are redacted where they are set',
    'graph/nodes/check_board.py::_pickup_todo_cards::active_sessions[item]': 'stores a session record built by the session writers, whose text fields are redacted where they are set',
    'graph/nodes/check_board.py::_handle_in_review_cards::active_sessions[item]': 'stores a session record built by the session writers, whose text fields are redacted where they are set',
    'graph/nodes/check_board.py::_handle_blocked_cards::_sessions_b[item]': 'stores a session record built by the session writers, whose text fields are redacted where they are set',
    'graph/nodes/dispatch_performer.py::inject_briefs::card_context[key]': 'the dispatch context sent to the performer, which is not a dashboard or board field',
    'graph/nodes/dispatch_performer.py::_stage_advance_gates::state[key]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'graph/nodes/dispatch_performer.py::_acquire_via_slot_manager::state[key]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'graph/nodes/dispatch_performer.py::_apply_observer_retune_overrides::card_context[knob]': 'a dispatch context knob with a bounded numeric or enumerated value, not performer text',
    'graph/nodes/dispatch_performer.py::_persona_scope_skip::state[key]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'graph/nodes/dispatch_performer.py::_documenting_gate_skip::state[key]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'graph/nodes/handle_blocked.py::handle_blocked::active_sessions[card_id]': 'stores a session record built by the session writers, whose text fields are redacted where they are set',
    'graph/nodes/monitor/body.py::_phase_terminal_success_s4_s1::state[key]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'graph/nodes/monitor/body.py::_phase_terminal_success_s5::state[key]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'graph/nodes/monitor/body.py::_phase_terminal_success_s2::state[key]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'graph/nodes/monitor/body.py::_phase_ephemeral_gate::state[_k]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'graph/nodes/monitor/verdict.py::_apply_pending_override::state[k]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'graph/nodes/monitor_pr.py::_mirror_closed_pr_ownership::session[key]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'graph/nodes/monitor_pr.py::monitor_pr::state[key]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'services/no_progress.py::note_no_progress::state[_KEY]': 'stores a per-stage counter dict of integers, not text',
    'services/no_progress.py::note_progress::state[_KEY]': 'stores a per-stage counter dict of integers, not text',
    'services/reconciliation.py::run_startup_reconciliation::by_session[sid]': 'stores a session record built by the session writers, whose text fields are redacted where they are set',
    'daemon.py::_invoke_single_graph::sessions[active_card_id]': 'stores a session record built by the session writers, whose text fields are redacted where they are set',
    'daemon.py::_mirror_board_metadata::state[_meta_dst]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'daemon.py::_merge_fanout_results::active_sessions[cid]': 'stores a session record built by the session writers, whose text fields are redacted where they are set',
    'daemon.py::_merge_session_results::active_sessions[result.card_id]': 'stores a session record built by the session writers, whose text fields are redacted where they are set',
    'daemon.py::_detect_stuck_performers::self._session_stuck_alerts[key]': 'stores a timestamp for each stuck-alert key, not text',
    'daemon.py::_collect_session_updates::updated_session[_f]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'daemon.py::_merge_fanout_results::state[k]': 'copies a value a writer already produced; the writers wrap the dict literal or the field at source',
    'daemon.py::_resolve_symphony_effective_state::_sym_sessions[cid]': 'stores a session record built by the session writers, whose text fields are redacted where they are set',
}
_APPEND_METHODS = {"append", "extend", "insert"}
_TEXT_NAME_SAMPLES = frozenset({*TEXT_FIELDS, "last_x_error"})


def _text_name(name: str) -> bool:
    return name in TEXT_FIELDS or any(fnmatch.fnmatchcase(name, glob) for glob in TEXT_GLOBS)


def _could_be_text(pattern: str) -> bool:
    """True when a name built from a pattern (``*`` for a formatted part) could be a text field."""
    return any(fnmatch.fnmatchcase(name, pattern) for name in _TEXT_NAME_SAMPLES) or any(
        fnmatch.fnmatchcase(pattern, glob) for glob in TEXT_GLOBS
    )


def _name_pattern(node: ast.expr) -> str | None:
    """The literal name a setattr call sets, with ``*`` for each formatted part; None if unknowable."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [
            value.value if isinstance(value, ast.Constant) else "*"
            for value in node.values
        ]
        pattern = "".join(str(part) for part in parts)
        return pattern if pattern.replace("*", "") else None
    return None


def _label_of(target: ast.expr) -> str:
    return ast.unparse(target)


_STATE_BASES = frozenset({"state", "sess", "override_result", "snap", "card_context"})


def _state_base(base: ast.expr) -> bool:
    """True for a state or session container: a bare name, or an attribute such as ``self._state``."""
    if isinstance(base, ast.Name):
        name = base.id
    elif isinstance(base, ast.Attribute):
        name = base.attr.lstrip("_")
    else:
        return False
    return name in _STATE_BASES or "session" in name


def _state_container(target: ast.Subscript) -> bool:
    """True for a write into a state or session container, where a text field can sit under a variable key."""
    return _state_base(target.value)


def _unwrapped_writers(tree: ast.AST) -> list[tuple[int, str]]:
    """Every write of a text field whose value does not go through the helper.

    A field write is labelled with the field name. A write whose key cannot be resolved
    is labelled with its target expression, and the test decides whether it is allow-listed.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                targets, value = [node.target], node.value
            if value is None:
                continue
            for target in targets:
                if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
                    if _text_name(str(target.slice.value)) and not _wrapped(value):
                        found.append((node.lineno, str(target.slice.value)))
                elif isinstance(target, ast.Subscript) and _state_container(target):
                    if not _wrapped(value):
                        found.append((node.lineno, _label_of(target)))
                elif isinstance(target, ast.Attribute) and _text_name(target.attr) and not _wrapped(value):
                    found.append((node.lineno, target.attr))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and _text_name(str(key.value)) and not _wrapped(value):
                    found.append((value.lineno, str(key.value)))
        elif isinstance(node, ast.Call):
            func = node.func
            is_log = isinstance(func, ast.Attribute) and func.attr in _LOGGER_METHODS
            for keyword in node.keywords:
                if keyword.arg and _text_name(keyword.arg) and not is_log and not _wrapped(keyword.value):
                    found.append((keyword.value.lineno, keyword.arg))
            if isinstance(func, ast.Name) and func.id == "setattr" and len(node.args) == 3:
                pattern = _name_pattern(node.args[1])
                if pattern is None:
                    if not _wrapped(node.args[2]):
                        found.append((node.lineno, f"setattr({_label_of(node.args[0])}, {_label_of(node.args[1])})"))
                elif _could_be_text(pattern) and not _wrapped(node.args[2]):
                    found.append((node.lineno, pattern))
            elif isinstance(func, ast.Attribute) and func.attr == "setdefault" and node.args:
                first = node.args[0]
                has_value = len(node.args) > 1
                if isinstance(first, ast.Constant):
                    if _text_name(str(first.value)) and has_value and not _wrapped(node.args[1]):
                        found.append((node.lineno, str(first.value)))
                elif _state_base(func.value) and has_value and not _wrapped(node.args[1]):
                    found.append((node.lineno, f"{_label_of(func.value)}.setdefault({_label_of(first)})"))
            elif isinstance(func, ast.Attribute) and func.attr in _APPEND_METHODS:
                receiver = func.value
                field = None
                if isinstance(receiver, ast.Subscript) and isinstance(receiver.slice, ast.Constant):
                    field = str(receiver.slice.value)
                elif isinstance(receiver, ast.Attribute):
                    field = receiver.attr
                if field is not None and _text_name(field):
                    for arg in node.args:
                        if not _wrapped(arg):
                            found.append((node.lineno, field))
    return found


def _is_allowed(rel: str, function: str, key: str) -> bool:
    """True when a write is allow-listed for exactly this file, this function and this key."""
    scoped = f"{rel}::{function}::{key}"
    return scoped in _DYNAMIC_WRITE_ALLOWANCES or scoped in _SAVE_PATH_COPIES


def _enclosing_function(tree: ast.AST, line: int) -> str:
    """The name of the innermost function that contains *line*, or "" at module level."""
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= line <= end and (best is None or node.lineno > best.lineno):
                best = node
    return best.name if best is not None else ""


def test_every_writer_of_a_text_field_routes_through_the_helper() -> None:
    offenders: list[str] = []
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(SRC))
        for line, key in _unwrapped_writers(tree):
            if _is_allowed(rel, _enclosing_function(tree, line), key):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{line} writes {key!r} unredacted")
    assert not offenders, "\n".join(offenders)


def test_a_length_bound_around_the_redaction_is_accepted_but_not_a_bound_alone() -> None:
    assert _unwrapped_writers(ast.parse("s.last_error = clip_session_error(redact_state_value(x))\n")) == []
    assert _unwrapped_writers(ast.parse("s.last_error = clip_session_error(x)\n")) == [(1, "last_error")]


def test_the_guard_catches_an_unwrapped_writer() -> None:
    tree = ast.parse("state['open_questions'] = [f'performer said {x}']\n")
    assert _unwrapped_writers(tree) == [(1, "open_questions")]
    assert _unwrapped_writers(ast.parse("state['open_questions'] = redact_state_value([x])\n")) == []
    assert _unwrapped_writers(ast.parse("state['open_questions'] = None\n")) == []


_GUARD_PROBES = [
    ("state['open_questions'] += [f'performer said {x}']\n", "open_questions"),
    ("state.setdefault('open_questions', [f'performer said {x}'])\n", "open_questions"),
    ("setattr(state, 'open_questions', f'performer said {x}')\n", "open_questions"),
    ("setattr(state, f'last_{role}_error', error)\n", "last_*_error"),
    ("state['open_questions'].append(f'performer said {x}')\n", "open_questions"),
    ("state['open_questions'].extend([f'performer said {x}'])\n", "open_questions"),
    ("state.update(open_questions=[f'performer said {x}'])\n", "open_questions"),
    ("state.update({'open_questions': [f'performer said {x}']})\n", "open_questions"),
    ("state.open_questions.append(f'performer said {x}')\n", "open_questions"),
    ("state[k] = text\n", "state[k]"),
    ("setattr(state, name, error)\n", "setattr(state, name)"),
]


@pytest.mark.parametrize(("snippet", "label"), _GUARD_PROBES)
def test_the_guard_flags_every_form_a_writer_can_take(snippet: str, label: str) -> None:
    found = [key for _, key in _unwrapped_writers(ast.parse(snippet))]
    assert found, f"the guard missed an unwrapped write: {snippet.strip()}"
    assert label in found[0] or found[0] == label, f"flagged {found!r}, expected {label!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        "state['open_questions'] += redact_state_value([f'x {y}'])\n",
        "state.setdefault('open_questions', redact_state_value([f'x {y}']))\n",
        "setattr(state, f'last_{role}_error', redact_state_value(error))\n",
        "state['open_questions'].append(redact_state_value(f'x {y}'))\n",
        "state.update(open_questions=redact_state_value([f'x {y}']))\n",
        "state.setdefault('stage_verdicts', {})\n",
        "setattr(state, 'agent_dispatch_at', None)\n",
    ],
)
def test_the_guard_passes_every_wrapped_or_non_text_form(snippet: str) -> None:
    assert _unwrapped_writers(ast.parse(snippet)) == []


def test_a_dynamic_write_is_flagged_until_it_is_allow_listed_with_a_reason() -> None:
    tree = ast.parse("state[k] = redact_state_value(v)\nsetattr(state, name, value)\n")
    labels = [key for _, key in _unwrapped_writers(tree)]
    assert "setattr(state, name)" in labels
    assert all(reason.strip() for reason in _DYNAMIC_WRITE_ALLOWANCES.values())


def test_the_secret_fixtures_are_what_the_redactor_is_meant_to_catch() -> None:
    """A fixture that the redactor missed would make the walk above pass for the wrong reason."""
    from coordinare.lib.redaction import redact_command_text

    cleaned = redact_command_text(_all_text())
    for value in SECRET_VALUES:
        assert value not in cleaned, f"the fixture {value[:6]}... is not redacted by redact_command_text"


def test_the_observer_evidence_string_is_redacted_on_record() -> None:
    verdict = parse_verdict({"data": {"verdict": sorted(OBSERVER_VERDICTS)[0], "reason": "ok"}})
    assert verdict is not None
    recorded = record_recent_verdict(None, verdict, [], {"note": SECRET_TEXTS["export"]})
    assert ENV_TOKEN not in json.dumps(recorded)


def test_the_stderr_buffer_holds_redacted_lines() -> None:
    transport = SubprocessTransport(executable="performer", timeout=5)
    proc: Any = _FakeProc([SECRET_TEXTS["curl"]])
    asyncio.run(transport._drain_stderr(proc))
    assert transport.agent_logs and CURL_PASSWORD not in "".join(transport.agent_logs)
