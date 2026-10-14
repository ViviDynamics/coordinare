"""Spec 129, sixth review: one failing test per blocker and follow-up, on the failing input.

Covers the redaction regressions from widening the descriptive suffixes (weak stems
must stay secret names, and the relief moves into the value check), the Bearer rule
after an auth-header key, the job secrets keyed by the run id that polls use, a non
transport drain error, teardown tracked by ``aclose``, and the refused set after a
busy or failed dispatch.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from coordinare.lib.transcript_redaction import redact_text, redact_value
from coordinare.models.performer_endpoint import JobStatus, PerformerEndpointConfig
from coordinare.services import http_performer_service as hps
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.run_transcript import TranscriptCollector, transcript_path
from coordinare.transport.http_transport import PerformerHTTPClient
from coordinare.workspace import WorkspaceInfo

SECRET_A = "hunter2hunter2"
SECRET_B = "abcdefghijklmnopqrstuvwx"


def _persistent_config() -> PerformerEndpointConfig:
    return PerformerEndpointConfig.model_validate(
        {
            "id": "perf-r6",
            "mode": "persistent",
            "roles": ["implementing"],
            "image": "performer:base",
            "endpoint": "http://127.0.0.1:8080",
        },
    )


def _card() -> dict[str, object]:
    return {
        "id": "card-r6",
        "role": "implementer",
        "backend": "claude_code",
        "persona_instructions": "be excellent",
    }


def _workspace(token: str) -> WorkspaceInfo:
    return WorkspaceInfo(
        path=None,
        repo_url="https://github.com/x/y",
        branch="main",
        github_token=token,
    )


# --- regressions from the widened descriptive suffixes ---------------------------------

REGRESSIONS: list[tuple[str, str]] = [
    ("token_url=hunter2hunter2", "hunter2hunter2"),
    ("api_key_file=abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
    ("cookie_path: abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
]


@pytest.mark.parametrize(("example", "secret"), REGRESSIONS)
def test_a_weak_stem_with_a_descriptive_suffix_still_redacts_a_secret_value(
    example: str,
    secret: str,
) -> None:
    assert secret not in redact_text(example)


def test_a_structured_credentials_path_with_a_secret_value_is_redacted() -> None:
    out = redact_value({"input": {"credentials_path": SECRET_A}})
    assert SECRET_A not in json.dumps(out)


WEAK_STEMS = ["token", "api_key", "auth_token", "credentials", "cookie", "secret", "session_token"]
DESCRIPTIVE_SUFFIXES = [
    "_url",
    "_file",
    "_path",
    "_dir",
    "_ttl",
    "_cache",
    "_expiry",
    "_timeout",
    "_header",
    "_name",
    "_count",
    "_limit",
    "_type",
    "_enabled",
    "_length",
    "_reset_sent",
    "_key_name",
    "_format",
    "_regex",
    "_prefix",
]
SECRET_VALUES = [SECRET_A, SECRET_B]


def _sweep_lines() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for stem in WEAK_STEMS:
        for suffix in DESCRIPTIVE_SUFFIXES:
            key = f"{stem}{suffix}"
            for value in SECRET_VALUES:
                cases.append((f"{key}={value}", value))
                cases.append((f"{key}: {value}", value))
                cases.append((f'{{"{key}": "{value}"}}', value))
    return cases


@pytest.mark.parametrize(("line", "secret"), _sweep_lines())
def test_secret_values_under_weak_stems_and_descriptive_suffixes_are_redacted(
    line: str,
    secret: str,
) -> None:
    assert secret not in redact_text(line), line


RELIEF: list[str] = [
    "token_ttl: 3600",
    "self.token_cache = {}",
    "credentials_file: /etc/x",
    "token: null",
    "bearer_auth = True",
    "token_url=https://api.example.com/oauth",
]


@pytest.mark.parametrize("example", RELIEF)
def test_round_five_relief_cases_are_still_kept(example: str) -> None:
    assert redact_text(example) == example


# --- F9: a Bearer token after an auth-header key, whatever its length -------------------


def test_a_short_bearer_token_after_an_auth_header_key_is_redacted() -> None:
    out = redact_text("auth_token_header: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out, out


def test_a_bearer_token_in_a_quoted_auth_header_value_is_redacted() -> None:
    out = redact_text('{"auth_header": "Bearer xyz123"}')
    assert "xyz123" not in out, out


# --- blocker 2: job secrets keyed by the run id the polls use ---------------------------


class _Client:
    """Accepts one job (persistent or ephemeral) and returns a terminal record with the secret."""

    def __init__(self, job_id: str, secret: str) -> None:
        self.job_id = job_id
        self.secret = secret

    async def post_job(self, payload: Any) -> Any:
        from coordinare.models.performer_endpoint import JobAcceptResponse

        return JobAcceptResponse(accepted=True, job_id=self.job_id, started_at=datetime.now(UTC))

    async def get_job(
        self,
        job_id: str,
        *,
        transcript_after: int | None = None,
        transcript_limit: int = 200,
    ) -> JobStatus:
        record = {
            "ts": "x",
            "run_id": job_id,
            "seq": 1,
            "type": "assistant_text",
            "text": f"the job used {self.secret} here",
        }
        after = int(transcript_after or 0)
        batch = [record] if after < 1 else []
        return JobStatus(
            job_id=job_id,
            state="succeeded",
            started_at=datetime.now(UTC),
            transcript=batch,
            transcript_last_seq=1,
        )

    async def post_reset(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(is_success=True, status_code=200)

    async def aclose(self) -> None:
        return None


async def test_a_persistent_job_secret_is_redacted_by_its_run_id(tmp_path: Path) -> None:
    root = tmp_path / "t"
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    client = _Client("job-p6", SECRET_A)
    svc = HTTPPerformerService(
        _persistent_config(),
        client=cast("PerformerHTTPClient", client),
        transcripts=collector,
    )
    await svc.dispatch_card(_card(), _workspace(SECRET_A))
    await svc.check_status(
        "job-p6",
        payload={"transcript_target": {"symphony": "alpha", "card_id": "PVTI_p6"}},
    )
    path = transcript_path(root, symphony="alpha", card_id="PVTI_p6", run_id="job-p6")
    text = path.read_text(encoding="utf-8")
    assert SECRET_A not in text
    assert svc._transcript_secrets == {}


async def test_an_ephemeral_job_secret_is_released_after_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinare.services.performer_runtime import StartedPerformer

    class _Runtime:
        async def start_ephemeral(self, config: Any, **kwargs: Any) -> StartedPerformer:
            return StartedPerformer(handle="pod-e6", endpoint="http://127.0.0.1:9")

        async def stop(self, handle: str, **kwargs: Any) -> None:
            return None

    async def fake_wait_ready(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(hps.performer_lifecycle, "wait_ready", fake_wait_ready)
    root = tmp_path / "t"
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    client = _Client("job-e6", SECRET_A)
    config = PerformerEndpointConfig.model_validate(
        {
            "id": "perf-e6",
            "mode": "ephemeral",
            "roles": ["implementing"],
            "image": "performer:base",
        },
    )
    svc = HTTPPerformerService(
        config,
        client=cast("PerformerHTTPClient", client),
        runtime=cast("Any", _Runtime()),
        transcripts=collector,
    )
    result = await svc.dispatch_card(_card(), _workspace(SECRET_A))
    session_id = result["session_id"]
    await svc.check_status(
        session_id,
        payload={"transcript_target": {"symphony": "alpha", "card_id": "PVTI_e6"}},
    )
    path = transcript_path(root, symphony="alpha", card_id="PVTI_e6", run_id=session_id)
    assert SECRET_A not in path.read_text(encoding="utf-8")
    assert svc._transcript_secrets == {}


# --- F6: a non transport error in the drain is retried, then the tail is marked --------


class _Garbage:
    def __init__(self, records: list[dict[str, Any]], last_seq: int, failures: int) -> None:
        self.records = records
        self.last_seq = last_seq
        self.failures = failures

    async def get_job(
        self,
        job_id: str,
        *,
        transcript_after: int | None = None,
        transcript_limit: int = 200,
    ) -> JobStatus:
        if self.failures > 0:
            self.failures -= 1
            raise ValueError("garbage state in the drain response")
        after = int(transcript_after or 0)
        batch = [r for r in self.records if r["seq"] > after]
        return JobStatus(
            job_id=job_id,
            state="succeeded",
            started_at=datetime.now(UTC),
            transcript=batch,
            transcript_last_seq=self.last_seq,
        )


async def test_a_garbage_drain_response_is_retried_then_marked_not_failed(tmp_path: Path) -> None:
    root = tmp_path / "t"
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    svc = HTTPPerformerService(_persistent_config(), client=None, transcripts=collector)
    writer = collector.writer_for(run_id="sess-garbage", symphony="alpha", card_id="PVTI_g")
    client = _Garbage([], last_seq=2, failures=10)
    status = JobStatus(
        job_id="sess-garbage",
        state="succeeded",
        started_at=datetime.now(UTC),
        transcript=[],
        transcript_last_seq=2,
    )
    await svc._drain_transcript(cast("PerformerHTTPClient", client), "sess-garbage", writer, status)
    path = transcript_path(root, symphony="alpha", card_id="PVTI_g", run_id="sess-garbage")
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines and lines[-1]["type"] == "truncated"
    assert writer.failed is False


async def test_a_garbage_drain_response_is_retried_once_before_giving_up(tmp_path: Path) -> None:
    root = tmp_path / "t"
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    svc = HTTPPerformerService(_persistent_config(), client=None, transcripts=collector)
    writer = collector.writer_for(run_id="sess-retry6", symphony="alpha", card_id="PVTI_r")
    client = _Garbage(
        [{"ts": "x", "run_id": "r", "seq": 1, "type": "assistant_text", "text": "m"}],
        1,
        1,
    )
    await svc._drain_transcript(cast("PerformerHTTPClient", client), "sess-retry6", writer, None)
    path = transcript_path(root, symphony="alpha", card_id="PVTI_r", run_id="sess-retry6")
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["seq"] for line in lines] == [1]


# --- F2: aclose waits for shielded teardown steps ---------------------------------------


async def test_aclose_waits_for_a_teardown_step_that_is_still_running() -> None:
    svc = HTTPPerformerService(_persistent_config(), client=None)
    done: list[int] = []

    async def step() -> None:
        await asyncio.sleep(0.2)
        done.append(1)

    task = asyncio.ensure_future(svc._run_teardown(step()))
    await asyncio.sleep(0)
    await svc.aclose()
    assert done == [1]
    await task


# --- F8: the refused set is released after a busy dispatch ------------------------------


async def test_a_busy_dispatch_in_a_conflicting_mount_releases_its_refused_entry(
    tmp_path: Path,
) -> None:
    from coordinare.models.performer_endpoint import VolumeMount

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"accepted": False, "reason": "busy"})

    client = PerformerHTTPClient(
        "http://127.0.0.1:8080",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    root = tmp_path / "t"
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    svc = HTTPPerformerService(_persistent_config(), client=client, transcripts=collector)
    mount = VolumeMount(host_path=root / "env", container_path="/env", mode="ro")
    for _ in range(3):
        result = await svc.dispatch_card(_card(), _workspace(SECRET_A), [mount])
        assert result["status"] == "error"
    assert svc._transcript_refused == set()
