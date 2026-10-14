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
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from coordinare.dashboard import DashboardStore
from coordinare.graph.nodes.monitor.body import (
    _BodyCtx,
    _phase_local_test_gate,
    _phase_review_routes_s2,
)
from coordinare.lib.redaction import redact_state_value
from coordinare.mcp.client import ToolResult
from coordinare.mcp.tools.state_tools import get_state_handler
from coordinare.services.observer import OBSERVER_VERDICTS, parse_verdict, record_recent_verdict
from coordinare.transport.subprocess_transport import SubprocessTransport

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

# The state keys whose values are free text that comes from a performer or a model.
TEXT_FIELDS = frozenset({
    "open_questions",
    "system_error_reason",
    "env_health_hold_reason",
    "convergence_reason",
    "env_blocked",
    "assessor_open_questions",
    "last_bootstrap_error",
    "last_error",
    "last_wiki_init_error",
})


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


def _seed_performer_state() -> dict[str, Any]:
    """Run the real entry points on secret-bearing performer output.

    Returns the state the daemon would hold after these writers ran, with the
    performer's stderr buffer under ``performer_logs``.
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

    # Performer stderr: the subprocess transport's buffer, the same one the dashboard reads.
    transport = SubprocessTransport(executable="performer", timeout=5)
    proc: Any = _FakeProc([SECRET_TEXTS["export"], SECRET_TEXTS["curl"], SECRET_TEXTS["bearer"]])
    asyncio.run(transport._drain_stderr(proc))
    state["performer_logs"] = transport.agent_logs
    return state


def _snapshot_from(state: dict[str, Any]) -> dict[str, Any]:
    """Build the dashboard snapshot for a live blocked session holding ``state``."""
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
                "performer_events": [],
            },
        },
        "open_questions": list(state.get("open_questions") or []),
        "system_error_reason": state.get("system_error_reason"),
        "observer_verdicts": list(state.get("observer_verdicts") or []),
        "env_blocked": state.get("env_blocked"),
        "env_health_hold_reason": state.get("env_health_hold_reason"),
    }
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = False
    daemon.running = True
    health = MagicMock()
    health.snapshot.return_value.probes = []
    return DashboardStore().build_snapshot(daemon, None, health)


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
        return value.func.id == "redact_state_value"
    if isinstance(value, ast.Constant):
        return True
    return isinstance(value, (ast.List, ast.Dict)) and not (value.elts if isinstance(value, ast.List) else value.keys)


def _unwrapped_writers(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in TEXT_FIELDS
                    and value is not None
                    and not _wrapped(value)
                ):
                    found.append((node.lineno, str(target.slice.value)))
                elif isinstance(target, ast.Attribute) and target.attr in TEXT_FIELDS and not _wrapped(value):
                    found.append((node.lineno, target.attr))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value in TEXT_FIELDS and not _wrapped(value):
                    found.append((value.lineno, str(key.value)))
        elif isinstance(node, ast.Call):
            is_log = isinstance(node.func, ast.Attribute) and node.func.attr in _LOGGER_METHODS
            for keyword in node.keywords:
                if keyword.arg in TEXT_FIELDS and not is_log and not _wrapped(keyword.value):
                    found.append((keyword.value.lineno, keyword.arg))
    return found


def test_every_writer_of_a_text_field_routes_through_the_helper() -> None:
    offenders: list[str] = []
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, key in _unwrapped_writers(tree):
            offenders.append(f"{path.relative_to(ROOT)}:{line} writes {key!r} unredacted")
    assert not offenders, "\n".join(offenders)


def test_the_guard_catches_an_unwrapped_writer() -> None:
    tree = ast.parse("state['open_questions'] = [f'performer said {x}']\n")
    assert _unwrapped_writers(tree) == [(1, "open_questions")]
    assert _unwrapped_writers(ast.parse("state['open_questions'] = redact_state_value([x])\n")) == []
    assert _unwrapped_writers(ast.parse("state['open_questions'] = None\n")) == []


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
