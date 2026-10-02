"""Unit tests for performer capability detection (spec 056, T018)."""

from __future__ import annotations

import pytest

from performer import capabilities


@pytest.fixture
def mock_paths(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Mutable allowlist of binaries pretended to be on PATH."""
    state: dict[str, bool] = {}

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if state.get(name, False) else None

    monkeypatch.setattr(capabilities.shutil, "which", fake_which)
    monkeypatch.setattr(capabilities, "_playwright_installed", lambda: False)
    return state


def test_no_backends_when_no_clis(mock_paths: dict[str, bool]) -> None:
    caps = capabilities.probe_capabilities()
    assert caps.backends == []


def test_only_present_backends_advertised(mock_paths: dict[str, bool]) -> None:
    mock_paths["claude"] = True
    mock_paths["codex"] = True
    caps = capabilities.probe_capabilities()
    assert set(caps.backends) == {"claude_code", "codex"}


def test_prime_agent_binary_is_advertised(mock_paths: dict[str, bool]) -> None:
    """Without this probe entry the backend is registered in the factory and
    config but never advertised, so PerformerPool rejects the endpoint."""
    mock_paths["prime-agent"] = True
    caps = capabilities.probe_capabilities()
    assert caps.backends == ["prime_agent"]


def test_every_probed_backend_is_a_known_backend_name(
    mock_paths: dict[str, bool],
) -> None:
    """Probe keys are backend identifiers, not binary names — a typo here would
    advertise a harness the factory cannot resolve."""
    from performer.backends import get_backend

    for name in capabilities._BACKEND_BINARIES:
        assert get_backend(name) is not None


def test_universal_tool_flag_advertised_when_binary_present(
    mock_paths: dict[str, bool],
) -> None:
    mock_paths["node"] = True
    caps = capabilities.probe_capabilities()
    assert "node" in caps.tool_flags


def test_browser_flag_requires_playwright(
    mock_paths: dict[str, bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    caps = capabilities.probe_capabilities()
    assert "browser" not in caps.tool_flags
    monkeypatch.setattr(capabilities, "_playwright_installed", lambda: True)
    caps = capabilities.probe_capabilities()
    assert "browser" in caps.tool_flags


def test_unknown_flags_ignored_for_forward_compat(mock_paths: dict[str, bool]) -> None:
    mock_paths["future_unknown_tool"] = True
    caps = capabilities.probe_capabilities()
    assert "future_unknown_tool" not in caps.tool_flags


def test_lint_flag_advertised_for_ruff(mock_paths: dict[str, bool]) -> None:
    mock_paths["ruff"] = True
    caps = capabilities.probe_capabilities()
    assert "lint" in caps.tool_flags


def test_lint_flag_advertised_for_pylint(mock_paths: dict[str, bool]) -> None:
    mock_paths["pylint"] = True
    caps = capabilities.probe_capabilities()
    assert "lint" in caps.tool_flags


def test_format_flag_advertised_for_black(mock_paths: dict[str, bool]) -> None:
    mock_paths["black"] = True
    caps = capabilities.probe_capabilities()
    assert "format" in caps.tool_flags


def test_format_flag_advertised_for_prettier(mock_paths: dict[str, bool]) -> None:
    mock_paths["prettier"] = True
    caps = capabilities.probe_capabilities()
    assert "format" in caps.tool_flags


def test_test_runner_flag_advertised_for_pytest(mock_paths: dict[str, bool]) -> None:
    mock_paths["pytest"] = True
    caps = capabilities.probe_capabilities()
    assert "test_runner" in caps.tool_flags


def test_test_runner_flag_advertised_for_jest(mock_paths: dict[str, bool]) -> None:
    mock_paths["jest"] = True
    caps = capabilities.probe_capabilities()
    assert "test_runner" in caps.tool_flags


def test_ruff_counts_for_both_lint_and_format(mock_paths: dict[str, bool]) -> None:
    mock_paths["ruff"] = True
    caps = capabilities.probe_capabilities()
    assert "lint" in caps.tool_flags
    assert "format" in caps.tool_flags


def test_no_lint_format_test_runner_without_binaries(mock_paths: dict[str, bool]) -> None:
    caps = capabilities.probe_capabilities()
    assert "lint" not in caps.tool_flags
    assert "format" not in caps.tool_flags
    assert "test_runner" not in caps.tool_flags
