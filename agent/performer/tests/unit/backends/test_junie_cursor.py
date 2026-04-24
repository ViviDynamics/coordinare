"""Unit tests for Junie and Cursor OpenCode-compatible backend adapters."""
from __future__ import annotations

from performer.backends.cursor import CursorBackend
from performer.backends.junie import JunieBackend


class TestJunieBackend:
    def test_default_executable(self) -> None:
        adapter = JunieBackend()
        assert adapter._executable == "junie"
        assert adapter._adapter_name == "junie"

    def test_env_override_executable(self, monkeypatch) -> None:
        monkeypatch.setenv("JUNIE_EXECUTABLE", "junie-cli")
        adapter = JunieBackend()
        assert adapter._executable == "junie-cli"


class TestCursorBackend:
    def test_default_executable(self) -> None:
        adapter = CursorBackend()
        assert adapter._executable == "cursor"
        assert adapter._adapter_name == "cursor"

    def test_env_override_executable(self, monkeypatch) -> None:
        monkeypatch.setenv("CURSOR_EXECUTABLE", "cursor-cli")
        adapter = CursorBackend()
        assert adapter._executable == "cursor-cli"
