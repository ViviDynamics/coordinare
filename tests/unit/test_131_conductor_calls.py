"""#131: the conductor's own model calls are transcribed, priced and listed (spec 131).

The wrapper writes one #129-format file per call under ``conductor/<card or system>/``. These
tests cover the record shape per call kind, the decision, the ledger, redaction, the disabled
mode, a failing writer, errors, the viewer's read of the file and the card's run list.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.config import CoordinareConfiguration
from coordinare.lib.path_segments import CONDUCTOR_FOLDER, is_conductor_folder, sanitize_segment
from coordinare.services import conductor_calls
from coordinare.services.conductor_calls import (
    CONDUCTOR_FOLDER as CALLS_FOLDER,
)
from coordinare.services.conductor_calls import (
    NULL_CALL_LOG,
    PROMPT_LIMIT_CHARS,
    ConductorCallLog,
    ModelReply,
    ModelRequest,
    ToolCall,
    assessment_decision,
    conductor_call,
)
from coordinare.services.run_transcript import TranscriptCollector
from coordinare.services.run_viewer import RunRef, list_runs, run_detail
from coordinare.services.token_usage import ModelPrice, PriceTable, TokenLedger, usage_block

CARD = "PVTI_131"
MODEL = "claude-test-131"
USAGE = {"input_tokens": 120, "output_tokens": 30, "cache_read": 8, "cache_write": 2}
PRICE = ModelPrice(input_per_million=3.0, output_per_million=15.0,
                   cache_read_per_million=0.3, cache_write_per_million=3.75)


def _log(root: Path, *, enabled: bool = True, ambient: dict[str, str] | None = None) -> ConductorCallLog:
    collector = TranscriptCollector(
        root, enabled=enabled, max_bytes_per_run=10**6, retention_days=0,
        ambient_secrets=ambient,
    )
    return ConductorCallLog(collector)


def _request(prompt: str = "decide this", system: str = "you decide") -> ModelRequest:
    return ModelRequest(model=MODEL, endpoint="test_endpoint", system=system, prompt=prompt,
                        parameters={"max_tokens": 64, "response_format": "json"})


def _reply(text: str = '{"ok": true}', *, usage: dict[str, int] | None = USAGE) -> ModelReply:
    return ModelReply(text=text, model=MODEL, usage=usage)


def _file(root: Path, card: str | None, call_id: str) -> Path:
    folder = card or "system"
    return root / CALLS_FOLDER / folder / f"{call_id}.jsonl"


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines()]


async def _scope_call(log: ConductorCallLog, root: Path, *, card: str | None = CARD) -> tuple[str, ModelReply]:
    async def send() -> ModelReply:
        return _reply('{"personas": {}}')

    async with conductor_call(log, kind="persona_scope", card_id=card, symphony="coordinare") as call:
        reply = await log.invoke(_request(), send)
        call.decide("scope: developer full")
    await log.drain()
    return call.call_id, reply


# --- the record shape, per call kind -------------------------------------------------------

_KINDS = [
    ("assess", CARD),
    ("assess_questions", CARD),
    ("feedback_classify", CARD),
    ("persona_scope", CARD),
    ("observer", CARD),
    ("config_assistant", None),
    ("env_manifest", None),
    ("env_signature", None),
    ("comment_classify", None),
    ("pr_comment_classify", None),
]


@pytest.mark.parametrize(("kind", "card"), _KINDS)
async def test_each_call_kind_writes_a_header_response_usage_and_decision(
    tmp_path: Path, kind: str, card: str | None,
) -> None:
    root = tmp_path / "t"
    log = _log(root)

    async def send() -> ModelReply:
        return _reply("assistant said hello")

    async with conductor_call(log, kind=kind, card_id=card, symphony="coordinare") as call:
        await log.invoke(_request(prompt="the prompt"), send)
        call.decide(f"{kind}: done")
    await log.drain()

    path = _file(root, card, call.call_id)
    lines = _lines(path)
    header = lines[0]
    assert header["type"] == "run_header"
    assert header["seq"] == 0
    assert header["conductor"]["kind"] == kind
    assert header["conductor"]["symphony"] == "coordinare"
    assert header["conductor"]["card_id"] == card
    assert header["conductor"]["decision"] == f"{kind}: done"
    assert header["conductor"]["parameters"] == {"max_tokens": 64, "response_format": "json"}
    assert header["prompt"]["rendered"] == "the prompt"
    assert header["prompt"]["system"] == "you decide"
    assert header["model"]["model"] == MODEL
    assert header["performer"]["endpoint"] == "test_endpoint"
    assert header["card"]["id"] == card

    records = lines[1:]
    assert [r["seq"] for r in records] == list(range(1, len(records) + 1))
    types = [r["type"] for r in records]
    assert types == ["assistant_text", "usage", "result"]
    assert records[0]["text"] == "assistant said hello"
    assert records[1]["usage"]["input_tokens"] == 120
    assert records[1]["stage"] == f"conductor:{kind}"
    assert records[2]["text"] == f"{kind}: done"
    assert records[2]["is_error"] is False


async def test_the_card_less_calls_are_written_under_system(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root)
    call_id, _ = await _scope_call(log, root, card=None)
    assert _file(root, None, call_id).is_file()
    assert not (root / CALLS_FOLDER / CARD).exists()


async def test_a_send_made_with_no_open_call_is_recorded_as_unattributed(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root)

    async def send() -> ModelReply:
        return _reply("untracked")

    reply = await log.invoke(_request(), send)
    await log.drain()
    assert reply.text == "untracked"
    files = list((root / CALLS_FOLDER / "system").glob("*.jsonl"))
    assert len(files) == 1
    header = _lines(files[0])[0]
    assert header["conductor"]["kind"] == "unattributed"
    assert _lines(files[0])[-1]["text"] == "no decision recorded"


async def test_a_call_that_never_sent_writes_no_file(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root)
    async with conductor_call(log, kind="assess", card_id=CARD) as call:
        call.decide("assessed: ready")
    await log.drain()
    assert not root.exists()


async def test_tool_calls_are_written_as_tool_call_records(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root)
    tool = ToolCall(name="emit_verdict", call_id="tu_1", input={"verdict": "continue"})

    async def send() -> ModelReply:
        return ModelReply(text="", model=MODEL, usage=None, tool_calls=(tool,))

    async with conductor_call(log, kind="observer", card_id=CARD) as call:
        await log.invoke(_request(), send)
        call.decide("observer: continue")
    await log.drain()
    records = _lines(_file(root, CARD, call.call_id))[1:]
    assert [r["type"] for r in records] == ["tool_call", "result"]
    assert records[0]["tool_name"] == "emit_verdict"
    assert records[0]["input"] == {"verdict": "continue"}


def test_the_wrapper_header_constants_match_the_run_header_module() -> None:
    from coordinare.services import run_header

    assert conductor_calls.RUN_HEADER_TYPE == run_header.RUN_HEADER_TYPE
    assert conductor_calls.RUN_HEADER_VERSION == run_header.RUN_HEADER_VERSION


def test_the_folder_is_the_one_name_the_sanitiser_never_produces() -> None:
    assert CALLS_FOLDER == CONDUCTOR_FOLDER == "_conductor"
    assert sanitize_segment(CONDUCTOR_FOLDER) != CONDUCTOR_FOLDER
    assert is_conductor_folder(CONDUCTOR_FOLDER)
    assert not is_conductor_folder("conductor")
    assert not is_conductor_folder("Conductor")


async def test_a_long_prompt_is_bounded_and_says_so(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root)
    long_prompt = "x" * (PROMPT_LIMIT_CHARS + 50)
    async with conductor_call(log, kind="assess", card_id=CARD) as call:
        await log.invoke(_request(prompt=long_prompt), lambda: _send_text("ok"))
        call.decide("assessed: ready")
    await log.drain()
    header = _lines(_file(root, CARD, call.call_id))[0]
    assert header["prompt"]["rendered"].startswith("x" * PROMPT_LIMIT_CHARS)
    assert "prompt truncated" in header["prompt"]["rendered"]
    assert len(header["prompt"]["rendered"]) < PROMPT_LIMIT_CHARS + 200


async def _send_text(text: str) -> ModelReply:
    return _reply(text)


# --- errors and timeouts -------------------------------------------------------------------


async def test_a_send_that_raises_is_recorded_as_an_error_and_re_raised(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root)

    async def send() -> ModelReply:
        raise TimeoutError("the model did not answer")

    with pytest.raises(TimeoutError):
        async with conductor_call(log, kind="persona_scope", card_id=CARD) as call:
            try:
                await log.invoke(_request(), send)
            finally:
                # The call sites decide in a finally, so a raised send still has a decision.
                call.decide("scope: fallback (timeout)")
    await log.drain()
    lines = _lines(_file(root, CARD, call.call_id))
    error = next(r for r in lines if r["type"] == "error")
    assert error["input"] == {"timed_out": True}
    assert "did not answer" in error["text"]
    result = lines[-1]
    assert result["is_error"] is True
    assert result["text"] == "scope: fallback (timeout)"


async def test_a_reply_that_reports_an_error_is_recorded_without_raising(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root)

    async def send() -> ModelReply:
        return ModelReply(error="http_503", timed_out=False)

    async with conductor_call(log, kind="assess", card_id=CARD) as call:
        reply = await log.invoke(_request(), send)
        call.decide("assessed: needs input (0 questions)")
    await log.drain()
    assert reply.error == "http_503"
    lines = _lines(_file(root, CARD, call.call_id))
    assert lines[-1]["is_error"] is True
    assert any(r["type"] == "error" and r["text"] == "http_503" for r in lines)


async def test_a_reply_without_usage_says_why_in_the_header(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root)

    async def send() -> ModelReply:
        return ModelReply(text="cli says hi", note="")

    async with conductor_call(log, kind="assess", card_id=CARD) as call:
        await log.invoke(_request(), send)
        call.decide("assessed: ready")
    await log.drain()
    lines = _lines(_file(root, CARD, call.call_id))
    assert lines[0]["conductor"]["usage_note"] == conductor_calls.NO_USAGE_REASON
    assert all(r["type"] != "usage" for r in lines)


# --- redaction -----------------------------------------------------------------------------


async def test_the_prompt_and_reply_are_redacted_before_they_touch_disk(tmp_path: Path) -> None:
    root = tmp_path / "t"
    secret = "sk-proj-131-live-secret-value-abcdef"
    log = _log(root, ambient={"OPENAI_API_KEY": secret})
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

    async def send() -> ModelReply:
        return _reply(f"the key is {secret} and the token {token}")

    async with conductor_call(log, kind="config_assistant") as call:
        await log.invoke(_request(prompt=f"config has {secret} and {token}"), send)
        call.decide("assistant: reply")
    await log.drain()
    raw = _file(root, None, call.call_id).read_text(encoding="utf-8")
    assert secret not in raw
    assert token not in raw


# --- the disabled mode and a failing writer ------------------------------------------------


async def test_disabled_transcripts_write_nothing_and_leave_the_reply_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root, enabled=False)
    assert log.enabled is False
    expected = _reply('{"sufficient": true}')

    async def send() -> ModelReply:
        return expected

    async with conductor_call(log, kind="assess", card_id=CARD) as call:
        reply = await log.invoke(_request(), send)
        call.decide("assessed: ready")
    await log.drain()
    assert reply is expected
    assert not root.exists()


async def test_the_null_log_passes_the_reply_through(tmp_path: Path) -> None:
    async def send() -> ModelReply:
        return _reply("plain")

    reply = await NULL_CALL_LOG.invoke(_request(), send)
    assert reply.text == "plain"
    assert NULL_CALL_LOG.enabled is False


async def test_a_failing_writer_does_not_fail_the_call_or_its_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "t"
    log = _log(root)
    assert log._collector is not None

    def broken_writer(**_: Any) -> Any:
        raise OSError("disk on fire")

    monkeypatch.setattr(log._collector, "new_writer", broken_writer)
    expected = _reply("answer")

    async def send() -> ModelReply:
        return expected

    async with conductor_call(log, kind="assess", card_id=CARD) as call:
        reply = await log.invoke(_request(), send)
        call.decide("assessed: ready")
    assert reply is expected
    assert call.decision == "assessed: ready"
    await log.drain()


async def test_a_writer_that_raises_mid_write_is_logged_and_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "t"
    log = _log(root)
    assert log._collector is not None
    original = log._collector.new_writer

    def writer_that_fails(**kwargs: Any) -> Any:
        writer = original(**kwargs)

        def boom(_records: Any, **__: Any) -> int:
            raise RuntimeError("write refused")

        monkeypatch.setattr(writer, "append_batch", boom)
        return writer

    monkeypatch.setattr(log._collector, "new_writer", writer_that_fails)
    async with conductor_call(log, kind="assess", card_id=CARD) as call:
        await log.invoke(_request(), lambda: _send_text("ok"))
        call.decide("assessed: ready")
    await log.drain()


async def test_a_failure_while_writing_does_not_escape_the_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "t"
    log = _log(root)
    assert log._collector is not None

    def broken(**_: Any) -> Any:
        raise RuntimeError("no collector today")

    monkeypatch.setattr(log._collector, "new_writer", broken)
    async with conductor_call(log, kind="assess", card_id=CARD):
        await log.invoke(_request(), lambda: _send_text("ok"))
    await log.drain()


# --- the ledger ----------------------------------------------------------------------------


async def test_the_ledger_counts_and_prices_the_call_under_its_card(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root)
    call_id, _ = await _scope_call(log, root)
    assert log._collector is not None
    ledger = log._collector.usage
    totals = ledger.totals(card=CARD)
    assert set(totals) == {MODEL}
    assert totals[MODEL].input_tokens == 120
    assert totals[MODEL].output_tokens == 30
    assert ledger.totals(stage="conductor:persona_scope")[MODEL].input_tokens == 120
    assert call_id in ledger.run_ids(card=CARD)

    table = PriceTable(rules={MODEL: PRICE}, source="table")
    block = usage_block(ledger, table, today=datetime.now(UTC).date(), symphonies=["coordinare"])
    # The call names its symphony, so its spend is in that symphony's figures (and not the bucket).
    per_symphony = block["by_symphony"]["coordinare"]["week"]
    assert per_symphony["models"][0]["priced"] is True
    assert per_symphony["tokens"]["input"] == 120
    assert block["conductor"]["week"]["tokens"]["total"] == 0


async def test_a_rebuild_from_disk_counts_the_same_calls(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root)
    await _scope_call(log, root)
    assert log._collector is not None
    live = log._collector.usage.totals(card=CARD)[MODEL]

    rebuilt = TokenLedger()
    rebuilt.load_from_transcripts(root, now=datetime.now(UTC))
    assert rebuilt.totals(card=CARD)[MODEL] == live
    # After a restart the call is attributed to the symphony its header names, as it was live.
    assert rebuilt.totals(symphony="coordinare")[MODEL] == live


async def test_the_disabled_mode_leaves_the_ledger_empty(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root, enabled=False)
    await _scope_call(log, root)
    assert not root.exists()


# --- the viewer and the card's run list ----------------------------------------------------


async def test_the_viewer_opens_a_conductor_call_and_the_card_lists_it(tmp_path: Path) -> None:
    root = tmp_path / "t"
    log = _log(root)
    call_id, _ = await _scope_call(log, root)

    listing = list_runs(root, symphony=None, card=CARD)
    row = next(r for r in listing["runs"] if r["run"] == call_id)
    assert row["symphony"] == CONDUCTOR_FOLDER
    assert row["stage"] == "conductor:persona_scope"
    assert row["conductor"] == {"kind": "persona_scope", "decision": "scope: developer full",
                                "symphony": "coordinare"}

    detail = run_detail(root, RunRef(symphony=CONDUCTOR_FOLDER, card=CARD, run=call_id))
    assert detail["header"]["type"] == "run_header"
    assert detail["summary"]["outcome"]["status"] == "completed"
    assert detail["summary"]["outcome"]["text"] == "scope: developer full"
    assert detail["summary"]["tokens"]["input"] == 120


def test_the_header_summary_of_a_performer_run_keeps_its_persona_stage() -> None:
    from coordinare.services.run_viewer import _header_summary

    summary = _header_summary({"persona": {"name": "implementing"}, "performer": {"backend": "claude"}})
    assert summary["stage"] == "implementing"
    assert summary["conductor"] is None


# --- decisions -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("assessment", "decision"),
    [
        ({"sufficient": True, "questions": []}, "assessed: ready"),
        ({"sufficient": False, "questions": ["a", "b"]}, "assessed: needs input (2 questions)"),
        ({"sufficient": False, "questions": ["a"]}, "assessed: needs input (1 question)"),
        ({"sufficient": True, "dependencies": [12, 40]}, "assessed: blocked by #12, #40"),
    ],
)
def test_the_assessment_decision_names_what_the_output_said(assessment: dict[str, Any], decision: str) -> None:
    assert assessment_decision(assessment) == decision


def test_a_decision_is_bounded() -> None:
    record = conductor_calls.CallRecord(kind="assess", card_id=CARD, symphony="", call_id="c",
                                        started_at=datetime.now(UTC))
    record.decide("x" * 2000)
    assert record.decision is not None
    assert len(record.decision) == conductor_calls.DECISION_LIMIT_CHARS


# --- config: the reserved folder name ------------------------------------------------------


def test_symphonies_named_conductor_load() -> None:
    for name in ("conductor", "Conductor", "_conductor"):
        symphonies: Any = [SimpleNamespace(name="coordinare"), SimpleNamespace(name=name)]
        assert CoordinareConfiguration.validate_unique_names(symphonies) == symphonies


async def test_a_symphony_named_like_the_conductor_keeps_its_runs_apart_from_the_calls(tmp_path: Path) -> None:
    """A symphony named ``conductor`` (or ``_conductor``) writes its runs to its own folder."""
    root = tmp_path / "t"
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6, retention_days=0)
    for name in ("conductor", "_conductor"):
        writer = collector.writer_for(run_id=f"run-{name}", symphony=name, card_id=CARD)
        writer.append_batch([{"ts": "2026-10-14T10:00:00+00:00", "seq": 1, "type": "assistant_text",
                              "text": "performer run"}], last_seq=1)
    log = ConductorCallLog(collector)
    call_id, _ = await _scope_call(log, root)
    collector.release("run-conductor")
    collector.release("run-_conductor")
    listing = list_runs(root, symphony=None, card=CARD)
    by_run = {row["run"]: row["symphony"] for row in listing["runs"]}
    assert by_run["run-conductor"] == "conductor"
    assert by_run["run-_conductor"].startswith("conductor-")
    assert by_run[call_id] == CONDUCTOR_FOLDER
    # The conductor's folder holds only the conductor's calls.
    assert {p.stem for p in (root / CONDUCTOR_FOLDER / CARD).glob("*.jsonl")} == {call_id}


def test_other_symphony_names_are_accepted() -> None:
    symphonies: Any = [SimpleNamespace(name="coordinare"), SimpleNamespace(name="conductor-tools")]
    assert CoordinareConfiguration.validate_unique_names(symphonies) == symphonies

