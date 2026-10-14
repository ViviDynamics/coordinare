"""148: the one-line primary argument of a tool call, for the activity feed.

The feed shows what a tool was asked to do ("Bash · uv run pytest -q"), not
only its name. A backend hands over the tool input it already has and gets
back the one argument that matters for that tool. The whole input is never
carried: a Write call's content, or an Edit's replacement text, stays here.
The daemon redacts the result again before the feed stores it.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

#: The longest argument the feed carries. The daemon clips to the same figure.
MAX_TOOL_ARG = 160

_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead"})
_FILE_KEYS = ("file_path", "notebook_path", "path")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _one_line(value: str) -> str:
    return " ".join(value.split())[:MAX_TOOL_ARG]


def _url_without_query(value: str) -> str:
    """The URL minus its query, fragment and any user:password@ part."""
    parts = urlsplit(value)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _file_path(tool_input: Mapping[str, Any]) -> str:
    for key in _FILE_KEYS:
        path = _text(tool_input.get(key))
        if path:
            return path
    return ""


def _grep_argument(tool_input: Mapping[str, Any]) -> str:
    pattern = _text(tool_input.get("pattern"))
    path = _text(tool_input.get("path"))
    return f"{pattern} in {path}" if pattern and path else pattern or path


def primary_argument(tool: str, tool_input: Any) -> str:
    """The argument worth showing for *tool*, or "" when none is.

    Bash: the command. Read/Edit/Write and their notebook kin: the file path.
    Grep: the pattern, and "in <path>" when a path was given. WebFetch: the
    URL without its query string. Every other tool shows nothing extra.
    """
    if not isinstance(tool_input, Mapping):
        return ""
    if tool == "Bash":
        return _one_line(_text(tool_input.get("command")))
    if tool in _FILE_TOOLS:
        return _one_line(_file_path(tool_input))
    if tool == "Grep":
        return _one_line(_grep_argument(tool_input))
    if tool == "WebFetch":
        url = _text(tool_input.get("url"))
        return _one_line(_url_without_query(url)) if url else ""
    return ""
