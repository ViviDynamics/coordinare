"""Spec 130, review of PR #205: the blocker and the follow-ups.

1. Persona history never blocks the event loop; a slow or failing write is reported.
2. A corrupt byte in the history file does not hide the history.
3. Persona text is stored redacted, and the header hash matches what it carries.
4. Env var names are recorded as names, and survive redaction.
5. The header is bounded on its encoded bytes and counts against the run budget.
6. The history root is resolved, like the state file.
7. Real root resolution and persistent-mode run identity are tested end to end.
8. A failed history write is reported in the PUT response; token edits say "token".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from coordinare.graph.nodes import dispatch_performer as dispatch_node
from coordinare.lib.transcript_redaction import redact_value
from coordinare.services import persona_history as ph
from coordinare.services import run_header as rh
from coordinare.services.run_transcript import (
    RunTranscriptWriter,
    TranscriptCollector,
    _to_line,
    transcript_path,
)
from tests.unit.test_130_persona_history import TOKEN, _client
from tests.unit.test_130_run_header import (
    CARD_ID,
    SYMPHONY,
    _dispatch_context,
    _DispatchClient,
    _lines,
    _rec,
    _run_inputs,
    _service,
)

_PERFORMER: dict[str, str | None] = {"id": "perf-205", "transport": "persistent", "endpoint": None}


def _config(state_file: Path) -> SimpleNamespace:
    return SimpleNamespace(state_file_path=state_file, performers=None)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _ascii_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str).encode("ascii")) + 1


# --- 1. the event loop is never blocked by persona history -------------------


async def test_a_slow_persona_write_does_not_block_the_loop_and_is_reported(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    monkeypatch.setattr(rh, "PERSONA_RECORD_WAIT_SECONDS", 0.05)
    real_fsync = os.fsync

    def slow_fsync(fd: int) -> None:
        time.sleep(1.0)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", slow_fsync)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    started = time.monotonic()
    try:
        inputs = await rh.build_run_inputs_bounded(
            config=_config(tmp_path / "state" / "coordinare.state.json"),
            role="architect",
            persona_sent="a changed persona",
            transcript_target={"symphony": SYMPHONY, "card_id": CARD_ID},
        )
    finally:
        task.cancel()
    elapsed = time.monotonic() - started
    assert ticks > 0, "the event loop did not tick while persona history was written"
    assert elapsed < 0.9, "the dispatch waited for the slow disk"
    assert inputs["persona"]["version"] is None
    assert "in time" in inputs["persona"]["reason"]
    assert inputs["routing"]["role"] == "architect"


async def test_a_failing_persona_write_is_reported_and_dispatch_proceeds(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    def failing_fsync(fd: int) -> None:
        raise OSError("disk gone (simulated)")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    inputs = await rh.build_run_inputs_bounded(
        config=_config(tmp_path / "state" / "coordinare.state.json"),
        role="architect",
        persona_sent="a changed persona",
        transcript_target={"symphony": SYMPHONY, "card_id": CARD_ID},
    )
    assert inputs["persona"]["version"] is None
    assert inputs["persona"]["reason"]


async def test_the_dispatch_node_awaits_the_bounded_inputs(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """The node's helper is a coroutine: a synchronous call would block the loop."""
    monkeypatch.setattr(rh, "persona_history_root", ph.persona_history_root)
    monkeypatch.chdir(tmp_path)
    state = {
        "config": _config(Path("state") / "coordinare.state.json"),
        "current_symphony": SYMPHONY,
    }
    ctx = {
        "card_context": {"persona_instructions": "Be precise." + rh.PERSONA_LANGUAGE_SUFFIX},
        "role": "architect",
        "card_id": CARD_ID,
    }
    awaitable = dispatch_node._dispatch_run_inputs(state, ctx)
    assert asyncio.iscoroutine(awaitable)
    inputs = await awaitable
    assert inputs["persona"]["version"] == 1


# --- 2. a corrupt byte does not hide the history --------------------------------


def test_a_corrupt_byte_does_not_hide_the_history(tmp_path: Path) -> None:
    root = tmp_path / "h"
    ph.PersonaHistory(root).record("architect", "v one", changed_by="local", source="dashboard")
    with (root / "architect.jsonl").open("ab") as handle:
        handle.write(b'\xff\xfe{"role":\n')
    restarted = ph.PersonaHistory(root)
    assert [v.version for v in restarted.versions("architect")] == [1]
    recorded = restarted.record("architect", "v two", changed_by="local", source="dashboard")
    assert recorded is not None and recorded.version == 2
    assert ph.reference_for_dispatch(root, "architect", "v two")["version"] == 2


def test_the_history_api_answers_200_over_a_corrupt_file(tmp_path: Path) -> None:
    client = _client(tmp_path)
    root = tmp_path / "state" / "persona_history"
    ph.PersonaHistory(root).record("architect", "v one", changed_by="local", source="dashboard")
    with (root / "architect.jsonl").open("ab") as handle:
        handle.write(b"\xff\xfe\n")
    response = client.get("/api/personas/architect/history")
    assert response.status_code == 200, response.text
    assert [v["version"] for v in response.json()["versions"]] == [1]


# --- 3. persona text is stored redacted --------------------------------------


def test_a_secret_in_persona_text_is_stored_redacted_and_hashed_as_stored(tmp_path: Path) -> None:
    root = tmp_path / "h"
    secret = "sk-" + "a" * 40
    store = ph.PersonaHistory(root)
    version = store.record(
        "architect", f"Use api_key = {secret} for the API.", changed_by="local", source="dashboard",
    )
    assert version is not None
    assert secret not in version.text
    assert secret not in (root / "architect.jsonl").read_text(encoding="utf-8")
    assert version.sha256 == _sha(version.text)


def test_an_unchanged_secret_text_does_not_create_a_second_version(tmp_path: Path) -> None:
    root = tmp_path / "h"
    secret = "sk-" + "b" * 40
    ph.PersonaHistory(root).record("architect", f"key {secret}", changed_by="local", source="dashboard")
    again = ph.reference_for_dispatch(root, "architect", f"key {secret}")
    assert again["version"] == 1


def test_the_header_hash_matches_the_redacted_sent_text_it_carries() -> None:
    secret = "sk-" + "c" * 40
    ctx = {"persona_instructions": f"Be exact. api_key = {secret}" + rh.PERSONA_LANGUAGE_SUFFIX}
    header = rh.build_run_header(
        run_id="r", card_context=ctx, run_inputs=_run_inputs(), performer=_PERFORMER,
    )
    sent = header["persona"]["sent_text"]
    assert secret not in sent
    assert header["persona"]["sent_text_sha256"] == _sha(sent)


def test_the_writer_rehashes_the_persona_text_after_exact_secret_redaction(tmp_path: Path) -> None:
    secret = "jobsecret-205-exact"
    header = rh.build_run_header(
        run_id="run-205",
        card_context={"persona_instructions": f"Use {secret} carefully."},
        run_inputs=_run_inputs(),
        performer=_PERFORMER,
    )
    root = tmp_path / "t"
    writer = RunTranscriptWriter(root, parts=("alpha", "PVTI_130"), run_id="run-205", max_bytes=10**6)
    writer.add_secret_values([secret])
    writer.stage_run_header(header)
    assert writer.flush_run_header() is True
    on_disk = _lines(root / "alpha" / "PVTI_130" / "run-205.jsonl")[0]["persona"]
    assert secret not in on_disk["sent_text"]
    assert on_disk["sent_text_sha256"] == _sha(on_disk["sent_text"])


# --- 4. env var names are names, and survive redaction -------------------------


def test_env_var_names_are_recorded_as_names_and_survive_redaction() -> None:
    ctx = {
        "model": "claude-sonnet",
        "api_key_env": "ANTHROPIC_API_KEY",
        "auth_token_env": "COORDINARE_PROXY_AUTH",
        "orchestration": {
            "strategy": "conditional",
            "tool": {"name": "t", "model": "m", "auth_env": "LITELLM_KEY_VAR"},
        },
    }
    header = rh.build_run_header(run_id="r", card_context=ctx, run_inputs=None, performer=_PERFORMER)
    redacted = redact_value(header, ())
    names = redacted["model"]["env_var_names"]
    assert names == {"api": "ANTHROPIC_API_KEY", "auth": "COORDINARE_PROXY_AUTH"}
    assert redacted["model_endpoints"]["tool"]["env_var_names"] == {"auth": "LITELLM_KEY_VAR"}
    for value in [*names.values(), "LITELLM_KEY_VAR"]:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", value), value


# --- 5. the header is bounded on its encoded bytes ----------------------------


def test_the_header_fits_the_bound_on_its_encoded_bytes_even_for_astral_text() -> None:
    astral = "\U0001F600" * 40000
    ctx = {
        "id": CARD_ID,
        "description": astral,
        "acceptance_criteria": [astral[:500]] * 50,
        "persona_instructions": astral,
        "relay_feedback": [{"body": astral[:8000]}] * 20,
    }
    header = rh.build_run_header(run_id="r", card_context=ctx, run_inputs={"persona": {"version": 1}}, performer=_PERFORMER)
    assert _ascii_bytes(header) <= rh.RUN_HEADER_MAX_BYTES
    assert header["card"]["truncated"] is True
    assert header["persona"]["sent_text"] is not None


def test_the_header_counts_against_the_run_byte_budget(tmp_path: Path) -> None:
    header = rh.build_run_header(
        run_id="run-205", card_context={"id": CARD_ID}, run_inputs=_run_inputs(), performer=_PERFORMER,
    )
    size = len(_to_line(header))
    root = tmp_path / "t"
    writer = RunTranscriptWriter(root, parts=("alpha", "PVTI_130"), run_id="run-205", max_bytes=size)
    writer.stage_run_header(header)
    writer.flush_run_header()
    writer.append_batch([_rec(1)], last_seq=1)
    lines = _lines(root / "alpha" / "PVTI_130" / "run-205.jsonl")
    assert lines[0]["type"] == "run_header"
    assert lines[1]["type"] == "truncated"


# --- 6. the history root is resolved -------------------------------------------


def test_the_history_root_is_resolved_like_the_state_file(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    root = ph.persona_history_root(_config(Path("state") / "coordinare.state.json"))
    assert root is not None
    assert root.is_absolute()
    assert root == (tmp_path / "state" / "persona_history").resolve()


# --- 7. real root resolution and persistent-mode identity ----------------------


async def test_a_dispatch_writes_its_persona_history_under_the_real_state_root(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    monkeypatch.setattr(rh, "persona_history_root", ph.persona_history_root)
    monkeypatch.chdir(tmp_path)
    state = {
        "config": _config(Path("state") / "coordinare.state.json"),
        "current_symphony": SYMPHONY,
    }
    ctx = {
        "card_context": {"persona_instructions": "Be precise." + rh.PERSONA_LANGUAGE_SUFFIX},
        "role": "architect",
        "card_id": CARD_ID,
    }
    inputs = await dispatch_node._dispatch_run_inputs(state, ctx)
    assert (tmp_path / "state" / "persona_history" / "architect.jsonl").is_file()
    client = _DispatchClient([_rec(1)], last_seq=1)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    result = await service.dispatch_card(_dispatch_context(), run_inputs=inputs)
    path = transcript_path(tmp_path / "t", symphony=SYMPHONY, card_id=CARD_ID, run_id=result["session_id"])
    header = _lines(path)[0]
    assert header["type"] == "run_header"
    assert header["persona"]["version"] == 1


async def test_persistent_mode_names_the_run_by_its_job_id(tmp_path: Path) -> None:
    client = _DispatchClient([_rec(1)], last_seq=1)
    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    service = _service(client, collector)
    result = await service.dispatch_card(_dispatch_context(), run_inputs=_run_inputs())
    run_id = result["job_id"]
    assert result["session_id"] == run_id
    path = transcript_path(tmp_path / "t", symphony=SYMPHONY, card_id=CARD_ID, run_id=run_id)
    assert path.is_file()
    assert _lines(path)[0]["run_id"] == run_id


# --- 8. a failed history write is reported; token edits say "token" -----------


def test_a_failed_history_write_is_reported_in_the_put_response(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    client = _client(tmp_path)
    headers = {"If-Match": client.get("/api/personas").headers["ETag"]}

    def failing_append(self: ph.PersonaHistory, path: Path, version: ph.PersonaVersion) -> None:
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(ph.PersonaHistory, "_append", failing_append)
    response = client.put(
        "/api/personas/architect", json={"instructions": "saved text"}, headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["warning"]
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["personas"]["architect"]["instructions"] == "saved text"  # the save went through


def test_a_token_authenticated_edit_is_attributed_to_token(tmp_path: Path) -> None:
    client = _client(tmp_path, token=TOKEN)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    etag = client.get("/api/personas", headers=auth).headers["ETag"]
    response = client.put(
        "/api/personas/architect",
        json={"instructions": "token edit"},
        headers={**auth, "If-Match": etag},
    )
    assert response.status_code == 200, response.text
    versions = client.get("/api/personas/architect/history", headers=auth).json()["versions"]
    assert versions[-1]["changed_by"] == "token"
