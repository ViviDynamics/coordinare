"""#131 review fixes: every backend records, the folder and the symphonies, the card in scope,
the decisions and the shutdown drain (spec 131).

The backends are built by ``build_conducting_backend``, as the daemon builds them, and the
model is faked at the SDK, the HTTP client or the subprocess. Each test asserts the file on
disk and, where the call carries usage, the ledger entry.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.lib.path_segments import CONDUCTOR_FOLDER
from coordinare.services import claude as claude_module
from coordinare.services import conductor_calls, run_viewer
from coordinare.services.conducting import build_conducting_backend
from coordinare.services.conductor_calls import (
    ConductorCallLog,
    ModelReply,
    ModelRequest,
    assessment_decision,
    conductor_call,
)
from coordinare.services.env_signature import match_env_signature_with_model
from coordinare.services.issue_comment_service import classify_issue_comment_ai
from coordinare.services.pr_conversation_feedback import _classify
from coordinare.services.run_transcript import TranscriptCollector
from coordinare.services.run_viewer import RunRef, list_runs, run_detail
from coordinare.services.token_usage import PriceTable, TokenLedger, usage_summary

CARD = "PVTI_131R"
KEY_ENV = "RV131_API_KEY"
MODEL = "model-131"
ASSESS_JSON = '{"sufficient": true, "questions": [], "rationale": "ok"}'


def _collector(root: Path) -> TranscriptCollector:
    return TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6, retention_days=0)


def _config(backend: str, **extra: Any) -> Any:
    conducting = SimpleNamespace(
        backend=backend, model=MODEL, max_tokens=64, temperature=None, api_key_env=KEY_ENV,
        base_url=None, executable=None, effort=None,
    )
    for key, value in extra.items():
        setattr(conducting, key, value)
    return SimpleNamespace(conducting=conducting)


def _files(root: Path, card: str = CARD) -> list[Path]:
    return sorted((root / CONDUCTOR_FOLDER / card).glob("*.jsonl"))


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines()]


# --- fakes at the model boundary ----------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text
        self.type = "text"


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 2
    cache_creation_input_tokens = 0


class _Response:
    content = (_Block(ASSESS_JSON),)
    usage = _Usage()
    model = MODEL


class _Messages:
    async def create(self, **_: Any) -> _Response:
        return _Response()


class FakeAsyncAnthropic:
    def __init__(self, **_: Any) -> None:
        self.messages = _Messages()


class _FakeProc:
    returncode = 0

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return ASSESS_JSON.encode(), b""

    def kill(self) -> None:
        return None

    async def wait(self) -> int:
        return 0


async def _fake_exec(*_: Any, **__: Any) -> _FakeProc:
    return _FakeProc()


async def _fake_post(self: httpx.AsyncClient, url: str, **_: Any) -> httpx.Response:
    body = {
        "choices": [{"message": {"content": ASSESS_JSON}}],
        "model": MODEL,
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 4}},
    }
    return httpx.Response(200, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, "key-131")
    monkeypatch.setattr(claude_module, "AsyncAnthropic", FakeAsyncAnthropic)
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)


# --- 1. every backend kind records ----------------------------------------------------------


@pytest.mark.parametrize(
    ("backend_kind", "endpoint", "usage_expected"),
    [
        ("anthropic_api", "anthropic_api", True),
        ("openai_api", "openai_api", True),
        ("claude_cli", "claude_cli", False),
        ("opencode", "opencode_cli", False),
        ("codex_cli", "codex_cli", False),
    ],
)
async def test_a_backend_built_by_the_factory_writes_its_call_and_counts_its_usage(
    tmp_path: Path, fakes: None, backend_kind: str, endpoint: str, usage_expected: bool,
) -> None:
    root = tmp_path / "t"
    collector = _collector(root)
    log = ConductorCallLog(collector)
    backend = build_conducting_backend(_config(backend_kind), calls=log)

    async with conductor_call(backend, kind="assess", card_id=CARD) as call:
        result = await backend.assess({"id": CARD, "title": "t", "body": "b"})
        call.decide(assessment_decision(result))
    await log.drain()

    files = _files(root)
    assert len(files) == 1, f"{backend_kind} wrote {len(files)} files"
    lines = _lines(files[0])
    assert lines[0]["conductor"]["kind"] == "assess"
    assert lines[0]["performer"]["endpoint"] == endpoint
    assert lines[-1]["type"] == "result"
    assert lines[-1]["text"] == "assessed: ready"
    assert lines[-1]["is_error"] is False
    if usage_expected:
        assert any(r["type"] == "usage" for r in lines)
        totals = collector.usage.totals(card=CARD)
        assert totals, f"{backend_kind}: the ledger counted nothing under the card"
        assert sum(t.input_tokens for t in totals.values()) > 0
    else:
        assert all(r["type"] != "usage" for r in lines)
        assert lines[0]["conductor"]["usage_note"] == conductor_calls.NO_USAGE_REASON
        assert collector.usage.totals(card=CARD) == {}


async def test_the_null_backend_makes_no_model_call_and_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = ConductorCallLog(_collector(root))
    backend = build_conducting_backend(_config("none"), calls=log)
    async with conductor_call(backend, kind="assess", card_id=CARD) as call:
        result = await backend.assess({"id": CARD})
        call.decide(assessment_decision(result))
    await log.drain()
    assert result["sufficient"] is True
    assert not root.exists()


async def test_a_call_opened_on_a_backend_without_a_log_is_recorded_by_the_send_inside_it(
    tmp_path: Path, fakes: None,
) -> None:
    """With no log on the outer backend, the wrapper must not hide the inner send's record."""
    root = tmp_path / "t"
    inner = ConductorCallLog(_collector(root))
    backend = build_conducting_backend(_config("anthropic_api"), calls=inner)
    bare = SimpleNamespace()  # a backend with no call log at all

    async with conductor_call(bare, kind="assess", card_id=CARD) as call:
        await backend.assess({"id": CARD})
        call.decide("assessed: ready")
    await inner.drain()

    files = _files(root, card="system")
    assert len(files) == 1, "the inner send wrote nothing"
    assert _lines(files[0])[0]["conductor"]["kind"] == "unattributed"


async def test_a_call_on_a_disabled_log_leaves_the_current_call_unset(tmp_path: Path) -> None:
    log = ConductorCallLog(_collector(tmp_path / "t"))
    log_disabled = ConductorCallLog(TranscriptCollector(tmp_path / "off", enabled=False,
                                                        max_bytes_per_run=10**6))
    async with log_disabled.call("assess", card_id=CARD):
        assert conductor_calls._CURRENT.get() is None
    assert log.enabled is True


# --- 2. the folder, the symphonies, the viewer address -------------------------------------


def test_the_conductor_folder_is_one_the_sanitiser_never_produces() -> None:
    from coordinare.lib.path_segments import sanitize_segment

    assert CONDUCTOR_FOLDER == "_conductor"
    for name in ("_conductor", "conductor", "Conductor", "__conductor", "_Conductor"):
        assert sanitize_segment(name) != CONDUCTOR_FOLDER


def test_the_viewer_admits_the_conductor_folder_and_no_other_underscore_name() -> None:
    assert run_viewer._valid_symphony_segment(CONDUCTOR_FOLDER)
    assert not run_viewer._valid_segment(CONDUCTOR_FOLDER)
    for name in ("_Conductor", "_other", "__conductor", "conductor-x"):
        if name == "conductor-x":
            assert run_viewer._valid_symphony_segment(name)
        else:
            assert not run_viewer._valid_symphony_segment(name)


async def test_symphonies_named_conductor_keep_their_runs_apart_from_the_calls(
    tmp_path: Path, fakes: None,
) -> None:
    """A symphony named like the conductor's folder writes to its own folder, not the calls'.

    ``conductor`` and ``_conductor`` sanitise to different folders on every filesystem. The pair
    ``conductor``/``Conductor`` is one folder on a case-insensitive filesystem, a limit of the
    sanitiser that predates this change, so it is not tested here.
    """
    root = tmp_path / "t"
    collector = _collector(root)
    rows = [{"ts": "2026-10-14T10:00:00+00:00", "seq": 1, "type": "assistant_text", "text": "run"}]
    for name in ("conductor", "_conductor"):
        writer = collector.writer_for(run_id=f"run-{name}", symphony=name, card_id=CARD)
        writer.append_batch(rows, last_seq=1)
        collector.release(f"run-{name}")
    log = ConductorCallLog(collector)
    backend = build_conducting_backend(_config("openai_api"), calls=log)
    async with conductor_call(backend, kind="assess", card_id=CARD, symphony="conductor") as call:
        await backend.assess({"id": CARD})
        call.decide("assessed: ready")
    await log.drain()

    listing = list_runs(root, symphony=None, card=CARD)
    by_run = {row["run"]: row for row in listing["runs"]}
    assert by_run["run-conductor"]["symphony"] == "conductor"
    assert by_run["run-_conductor"]["symphony"].startswith("conductor-")
    assert by_run[call.call_id]["symphony"] == CONDUCTOR_FOLDER
    # The call opens under the conductor's folder, and its header names the symphony it was for.
    # 211: the header's usage is the ledger's figures for the call.
    usage = {"tracked": True, "reason": None, **usage_summary(
        collector.usage.totals(run_id=call.call_id), PriceTable(rules={}, source="none"),
    )}
    detail = run_detail(root, RunRef(symphony=CONDUCTOR_FOLDER, card=CARD, run=call.call_id), usage=usage)
    assert detail["header"]["conductor"]["symphony"] == "conductor"
    # Its spend is attributed to that symphony on the ledger.
    assert collector.usage.totals(symphony="conductor")


def test_a_symphony_named_conductor_loads_from_config() -> None:
    from coordinare.config import CoordinareConfiguration

    symphonies: Any = [SimpleNamespace(name="coordinare"), SimpleNamespace(name="conductor"),
                       SimpleNamespace(name="Conductor"), SimpleNamespace(name="_conductor")]
    assert CoordinareConfiguration.validate_unique_names(symphonies) == symphonies


# --- 3. the card is in scope for the three call sites ------------------------------------


class _LoggedBackend:
    """A backend that sends through a call log, as the real ones do."""

    def __init__(self, calls: ConductorCallLog, reply: str) -> None:
        self.calls = calls
        self._reply = reply

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        async def send() -> ModelReply:
            return ModelReply(text=self._reply, model=MODEL,
                              usage={"input_tokens": 7, "output_tokens": 3, "cache_read": 0, "cache_write": 0})

        reply = await self.calls.invoke(ModelRequest(model=MODEL, endpoint="ep", system="", prompt=text), send)
        return {"text": reply.text, "data": json.loads(reply.text)}


async def test_a_comment_classified_for_a_card_is_listed_on_it_and_counts_against_it(tmp_path: Path) -> None:
    root = tmp_path / "t"
    collector = _collector(root)
    log = ConductorCallLog(collector)
    backend = _LoggedBackend(log, '{"label": "scope_change", "rationale": "r"}')
    label = await classify_issue_comment_ai("please change scope", "alice", backend, card_id=CARD)
    await log.drain()
    assert label == "scope_change"
    assert len(_files(root)) == 1, "the comment classification wrote no file on the card"
    assert _lines(_files(root)[0])[0]["card"]["id"] == CARD
    assert collector.usage.totals(card=CARD)
    runs = list_runs(root, symphony=None, card=CARD)["runs"]
    assert [row["stage"] for row in runs] == ["conductor:comment_classify"]


async def test_a_pr_comment_classified_for_a_card_is_listed_on_it(tmp_path: Path) -> None:
    root = tmp_path / "t"
    collector = _collector(root)
    log = ConductorCallLog(collector)
    backend = _LoggedBackend(log, '{"classification": "request"}')
    assert await _classify("please fix it", "bob", backend, card_id=CARD) is True
    await log.drain()
    header = _lines(_files(root)[0])[0]
    assert header["conductor"]["kind"] == "pr_comment_classify"
    assert header["card"]["id"] == CARD
    assert collector.usage.totals(card=CARD)


async def test_an_env_signature_judged_for_a_card_is_listed_on_it(tmp_path: Path) -> None:
    root = tmp_path / "t"
    collector = _collector(root)
    log = ConductorCallLog(collector)
    backend = _LoggedBackend(log, '{"is_environmental": true, "pattern_id": "quota", "reason": "r"}')
    cause = await match_env_signature_with_model("disk full", [], backend, card_id=CARD)
    await log.drain()
    assert cause is not None
    header = _lines(_files(root)[0])[0]
    assert header["conductor"]["kind"] == "env_signature"
    assert header["card"]["id"] == CARD
    assert collector.usage.totals(card=CARD)


# --- 4. decisions never change a card's outcome -------------------------------------------


@pytest.mark.parametrize("junk", [None, "not a dict", 7, ["x"], {"sufficient": False, "questions": 5}])
def test_an_assessment_decision_never_raises(junk: Any) -> None:
    decision = assessment_decision(junk)
    assert isinstance(decision, str)
    assert decision


def test_a_decision_that_cannot_be_written_is_recorded_as_unavailable() -> None:
    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("no text for you")

    record = conductor_calls.CallRecord(kind="assess", card_id=CARD, symphony="",
                                        call_id="c", started_at=datetime.now(UTC))
    value: Any = Unprintable()
    record.decide(value)
    assert record.decision == conductor_calls.DECISION_UNAVAILABLE


async def test_a_failing_write_never_reaches_the_body_of_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = ConductorCallLog(_collector(tmp_path / "t"))

    def broken_write(record: Any) -> None:
        raise RuntimeError("the write failed")

    monkeypatch.setattr(log, "_persist", broken_write)
    async with log.call("assess", card_id=CARD) as call:
        await log.invoke(ModelRequest(model="m", endpoint="e", system="", prompt="p"),
                         lambda: _reply_text("ok"))
        call.decide("assessed: ready")
    await log.drain()
    assert call.decision == "assessed: ready"


# --- 5. the shutdown drain and the bounded listing ----------------------------------------


async def test_the_shutdown_drain_lands_pending_calls(tmp_path: Path, fakes: None) -> None:
    root = tmp_path / "t"
    log = ConductorCallLog(_collector(root))
    backend = build_conducting_backend(_config("openai_api"), calls=log)
    async with conductor_call(backend, kind="assess", card_id=CARD) as call:
        await backend.assess({"id": CARD})
        call.decide("assessed: ready")
    fake: Any = SimpleNamespace(_state={"conductor_calls": log}, _state_store=None,
                                _lifecycle_signature=lambda: None)
    await CoordinareDaemon._shutdown_flush(fake, None)
    assert not log._pending
    assert len(_files(root)) == 1


async def test_a_drain_with_a_bound_returns_while_a_write_is_still_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "t"
    collector = _collector(root)
    log = ConductorCallLog(collector)
    release = threading.Event()

    def stuck(record: Any) -> None:
        release.wait(timeout=30)

    monkeypatch.setattr(log, "_persist", stuck)
    async with log.call("assess", card_id=CARD) as call:
        await log.invoke(ModelRequest(model="m", endpoint="e", system="", prompt="p"),
                         lambda: _reply_text("ok"))
        call.decide("assessed: ready")
    await log.drain(within=0.05)
    assert log._pending, "the bounded drain should return with the write still pending"
    release.set()
    await log.drain()


async def _reply_text(text: str) -> ModelReply:
    return ModelReply(text=text)


def _runs_file(root: Path, card: str, run: str, when: float) -> Path:
    path = root / "sym" / card / f"{run}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "run_header", "seq": 0, "ts": "2026-10-14T10:00:00+00:00", "run_id": run}
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    os.utime(path, (when, when))
    return path


def test_the_listing_reads_headers_only_for_the_newest_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "t"
    for index in range(10):
        _runs_file(root, CARD, f"run-{index:02d}", when=1_000_000 + index)
    monkeypatch.setattr(run_viewer, "LIST_MAX_RUNS", 3)
    reads: list[str] = []
    real = run_viewer._header_of

    def counting(path: Path, ref: RunRef) -> Any:
        reads.append(ref.run)
        return real(path, ref)

    monkeypatch.setattr(run_viewer, "_header_of", counting)
    listing = list_runs(root, symphony="sym", card=CARD)
    assert [row["run"] for row in listing["runs"]] == ["run-09", "run-08", "run-07"]
    assert listing["truncated"] is True
    assert sorted(reads) == ["run-07", "run-08", "run-09"], "headers were read beyond the cap"


def test_the_ledger_rebuild_uses_the_header_symphony_for_conductor_files(tmp_path: Path) -> None:
    root = tmp_path / "t"
    folder = root / CONDUCTOR_FOLDER / CARD
    folder.mkdir(parents=True)
    header = {"type": "run_header", "seq": 0, "ts": datetime.now(UTC).isoformat(), "run_id": "c1",
              "conductor": {"kind": "assess", "symphony": "coordinare"}}
    usage = {"type": "usage", "seq": 1, "ts": datetime.now(UTC).isoformat(), "scope": "message",
             "stage": "conductor:assess", "usage": {"input_tokens": 9, "output_tokens": 1,
                                                    "cache_read": 0, "cache_write": 0, "model": MODEL}}
    (folder / "c1.jsonl").write_text(json.dumps(header) + "\n" + json.dumps(usage) + "\n", encoding="utf-8")
    ledger = TokenLedger()
    ledger.load_from_transcripts(root, now=datetime.now(UTC))
    assert ledger.totals(symphony="coordinare")[MODEL].input_tokens == 9
    assert ledger.totals(symphony=CONDUCTOR_FOLDER) == {}
