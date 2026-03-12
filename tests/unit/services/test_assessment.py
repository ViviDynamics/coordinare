from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinare.services.assessment import (
    AnthropicApiBackend,
    ClaudeCliBackend,
    NullBackend,
    OpenCodeBackend,
    build_assessment_backend,
)

# ---------------------------------------------------------------------------
# NullBackend
# ---------------------------------------------------------------------------

class TestNullBackend:
    @pytest.mark.asyncio
    async def test_always_sufficient(self) -> None:
        result = await NullBackend().assess({"title": "anything"})
        assert result["sufficient"] is True

    @pytest.mark.asyncio
    async def test_rationale(self) -> None:
        result = await NullBackend().assess({})
        assert result["rationale"] == "assessment disabled"

    @pytest.mark.asyncio
    async def test_empty_questions(self) -> None:
        result = await NullBackend().assess({})
        assert result["questions"] == []


# ---------------------------------------------------------------------------
# AnthropicApiBackend
# ---------------------------------------------------------------------------

class TestAnthropicApiBackend:
    @pytest.mark.asyncio
    async def test_delegates_to_claude_service(self) -> None:
        expected = {"sufficient": True, "questions": [], "rationale": "ok"}
        svc = MagicMock()
        svc.assess_card_sufficiency = AsyncMock(return_value=expected)
        backend = AnthropicApiBackend(svc)

        result = await backend.assess({"title": "card"})

        svc.assess_card_sufficiency.assert_awaited_once_with({"title": "card"})
        assert result == expected

    @pytest.mark.asyncio
    async def test_propagates_exceptions(self) -> None:
        svc = MagicMock()
        svc.assess_card_sufficiency = AsyncMock(side_effect=RuntimeError("api down"))
        backend = AnthropicApiBackend(svc)

        with pytest.raises(RuntimeError, match="api down"):
            await backend.assess({})


# ---------------------------------------------------------------------------
# ClaudeCliBackend
# ---------------------------------------------------------------------------

def _make_proc(stdout: bytes, returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode
    return proc


class TestClaudeCliBackend:
    @pytest.mark.asyncio
    async def test_successful_json_response(self) -> None:
        payload = json.dumps({"sufficient": True, "questions": [], "rationale": "good"}).encode()
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(payload)):
            result = await ClaudeCliBackend().assess({"title": "card"})
        assert result["sufficient"] is True
        assert result["rationale"] == "good"

    @pytest.mark.asyncio
    async def test_heuristic_fallback_sufficient(self) -> None:
        text = b'"sufficient": true, everything looks good'
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(text)):
            result = await ClaudeCliBackend().assess({})
        assert result["sufficient"] is True

    @pytest.mark.asyncio
    async def test_heuristic_fallback_insufficient(self) -> None:
        text = b"Needs more detail in the acceptance criteria"
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(text)):
            result = await ClaudeCliBackend().assess({})
        assert result["sufficient"] is False

    @pytest.mark.asyncio
    async def test_empty_stdout_returns_insufficient(self) -> None:
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(b"")):
            result = await ClaudeCliBackend().assess({})
        assert result["sufficient"] is False

    @pytest.mark.asyncio
    async def test_timeout_returns_insufficient(self) -> None:
        proc = _make_proc(b"")
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await ClaudeCliBackend().assess({})
        assert result["sufficient"] is False
        assert "timeout" in result["rationale"]

    @pytest.mark.asyncio
    async def test_oserror_returns_insufficient(self) -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("not found")):
            result = await ClaudeCliBackend().assess({})
        assert result["sufficient"] is False
        assert "cli error" in result["rationale"]

    @pytest.mark.asyncio
    async def test_correct_cli_args(self) -> None:
        payload = json.dumps({"sufficient": True, "questions": [], "rationale": "ok"}).encode()
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(payload)) as mock_exec:
            await ClaudeCliBackend(executable="claude").assess({"title": "t"})
        args = mock_exec.call_args[0]
        assert args[0] == "claude"
        assert args[1] == "--print"


# ---------------------------------------------------------------------------
# OpenCodeBackend
# ---------------------------------------------------------------------------

class TestOpenCodeBackend:
    @pytest.mark.asyncio
    async def test_successful_json_response(self) -> None:
        payload = json.dumps({"sufficient": True, "questions": [], "rationale": "ok"}).encode()
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(payload)):
            result = await OpenCodeBackend().assess({"title": "card"})
        assert result["sufficient"] is True

    @pytest.mark.asyncio
    async def test_correct_cli_args(self) -> None:
        payload = json.dumps({"sufficient": True, "questions": [], "rationale": "ok"}).encode()
        with patch("asyncio.create_subprocess_exec", return_value=_make_proc(payload)) as mock_exec:
            await OpenCodeBackend(executable="opencode").assess({"title": "t"})
        args = mock_exec.call_args[0]
        assert args[0] == "opencode"
        assert args[1] == "run"
        # prompt is passed as the next positional arg (no --print flag)
        assert args[2] != "--print"

    @pytest.mark.asyncio
    async def test_timeout_returns_insufficient(self) -> None:
        proc = _make_proc(b"")
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await OpenCodeBackend().assess({})
        assert result["sufficient"] is False
        assert "timeout" in result["rationale"]

    @pytest.mark.asyncio
    async def test_oserror_returns_insufficient(self) -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("not found")):
            result = await OpenCodeBackend().assess({})
        assert result["sufficient"] is False


# ---------------------------------------------------------------------------
# build_assessment_backend factory
# ---------------------------------------------------------------------------

def _cfg(backend: str) -> MagicMock:
    cfg = MagicMock()
    cfg.assessment_backend = backend
    return cfg


class TestBuildAssessmentBackend:
    def test_none_returns_null_backend(self) -> None:
        assert isinstance(build_assessment_backend(_cfg("none")), NullBackend)

    def test_claude_cli_returns_claude_cli_backend(self) -> None:
        assert isinstance(build_assessment_backend(_cfg("claude_cli")), ClaudeCliBackend)

    def test_opencode_returns_opencode_backend(self) -> None:
        assert isinstance(build_assessment_backend(_cfg("opencode")), OpenCodeBackend)

    def test_anthropic_api_returns_anthropic_api_backend(self) -> None:
        result = build_assessment_backend(_cfg("anthropic_api"))
        assert isinstance(result, AnthropicApiBackend)

    def test_unknown_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown assessment_backend"):
            build_assessment_backend(_cfg("foobar"))
