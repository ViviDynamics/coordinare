"""Static admission rules that need no probe evidence: each names records by id and is corrected during discovery.

Kept out of models.py so that load-time validation of persisted manifests is unchanged. Discovery applies these to every
candidate, so the author that owns a record is told about its defect before any execution.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from .models import _graph_error

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .models import Activation, Command

_SHELLS = frozenset({"bash", "sh", "dash", "zsh"})
# Shell words that only shape control flow or print; a reachability test is judged on the other words of its command.
_INERT = frozenset({"set", "echo", "true", "false", ":", "exit", "for", "while", "until", "test", "[", "[[", "if", "then",
                    "else", "elif", "fi", "do", "done", "in"})
_LEADING = frozenset({"do", "then", "else", "if", "while", "until", "!"})
_SEPARATOR = re.compile(r"&&|\|\||[\n;|{}()]")
_HEAD_FLAG = re.compile(r"-[A-Za-z]*I[A-Za-z]*")
_CACHE = "{cache}/"
_CACHE_PATH = re.compile(r"\{cache\}[^\s'\"`;&|()<>]*")


def _script(argv: Sequence[str]) -> str:
    if len(argv) == 3 and argv[0].rsplit("/", 1)[-1] in _SHELLS and argv[1] == "-c":
        return argv[2]
    return shlex.join(argv)


def _reachability(words: list[str]) -> bool:
    """Only the network is touched: a HEAD request, output discarded, or ping, or nc -z."""
    tool = words[0]
    if tool == "ping":
        return True
    if tool == "nc":
        return any(word.startswith("-") and not word.startswith("--") and "z" in word for word in words[1:])
    if tool in {"curl", "wget"}:
        return any("/dev/null" in word or _HEAD_FLAG.fullmatch(word) or word in {"--head", "--spider"} for word in words[1:])
    return False


def connectivity_probe(command: Command) -> bool:
    """True when the command's only effect is a reachability test (v40 get.helm.sh gate); any other command disqualifies it."""
    try:
        segments = [words for words in (_strip(shlex.split(text)) for text in _SEPARATOR.split(_script(command.argv))) if words]
    except ValueError:
        return False
    tools = [words for words in segments if words[0] not in _INERT]
    return bool(tools) and all(_reachability(words) for words in tools)


def _strip(words: list[str]) -> list[str]:
    while words and words[0] in _LEADING:
        words = words[1:]
    return words


def _mentions(commands: Iterable[Command]) -> set[str]:
    """Cache paths an install names in argv, cwd or env; a path an install never names is never created by it."""
    found: set[str] = set()
    for command in commands:
        for text in [*command.argv, command.cwd, *command.env.values()]:
            for match in _CACHE_PATH.findall(text):
                for piece in match.split(":"):
                    piece = piece.rstrip("/")
                    if piece.startswith(_CACHE) and "$" not in piece and "{" not in piece[len(_CACHE):]:
                        found.add(piece)
    return found


def _templated(path: str) -> bool:
    return not path.startswith(_CACHE) or "$" in path or "{" in path[len(_CACHE):]


def _related(path: str, mention: str) -> bool:
    """An install creates or populates a path it names, names something inside it, or extracts into a directory above it."""
    return mention == path or mention.startswith(path + "/") or path.startswith(mention + "/")


_ARCHITECTURE = re.compile(r"(?<![a-z0-9])(amd64|x86_64|x64|arm64|aarch64)(?![a-z0-9])")
_CANONICAL = {"amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}
_FETCH = frozenset({"curl", "wget", "aria2c"})
# A command that detects the running CPU chooses its artifact at run time; only a fixed download is judged.
_DETECTS = re.compile(r"\bcase\b|\buname\b|--print-architecture")


def _names(words: list[str]) -> set[str]:
    return {_CANONICAL[match.group(1)] for word in words for match in _ARCHITECTURE.finditer(word)}


def foreign_artifact(command: Command, platform: str) -> bool:
    """A fixed download of a release artifact named for another CPU than the install platform.

    A segment that names the install platform's architecture is never judged, and neither is a command that detects
    the running CPU; an image platform flag such as --platform linux/amd64 is not a fetch.
    """
    native = platform.rsplit("/", 1)[-1]
    text = _script(command.argv)
    if _DETECTS.search(text):
        return False
    try:
        segments = [words for words in (shlex.split(part) for part in _SEPARATOR.split(text)) if words]
    except ValueError:
        return False
    return any((words[0] in _FETCH or (words[0] == "gh" and "download" in words))
               and (names := _names(words)) and native not in names for words in segments)


def validate_static_rules(install: Sequence[Command], activation: Activation, platform: str | None = None) -> None:
    """Raises the first correctable static error; each names its record ids (activation entries by position)."""
    gates = {command.id for command in install if connectivity_probe(command)}
    for command in install:
        if hit := gates & set(command.depends_on):
            raise _graph_error("connectivity_gate", "a reachability test cannot be a prerequisite", command.id, hit)
    for command in install if platform is not None else ():
        if foreign_artifact(command, platform):
            raise _graph_error("foreign_architecture_artifact", "a release artifact is built for another CPU architecture",
                               command.id, set())
    mentioned = _mentions(install)
    uncreated = {f"activation-path-{index}" for index, path in enumerate(activation.path)
                 if not _templated(path) and not any(_related(path, mention) for mention in mentioned)}
    if uncreated:
        raise _graph_error("activation_path_uncreated", "an activation path is not created by any install command",
                           "activation", uncreated)

