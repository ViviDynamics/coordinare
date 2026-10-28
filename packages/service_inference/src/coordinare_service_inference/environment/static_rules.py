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


# A package manager installing into the system: refused by a carrier without Linux capabilities (apt cannot drop
# privileges or write its system directories). Downloading or extracting a package into {cache} is not judged.
_PACKAGE_INSTALL = {"apt-get": {"install"}, "apt": {"install"}, "aptitude": {"install"}, "dpkg": {"-i", "--install"},
                    "apk": {"add"}, "yum": {"install"}, "dnf": {"install"}, "microdnf": {"install"},
                    "zypper": {"install", "in"}}
_PREFIXES = frozenset({"sudo", "env", "command", "exec", "nice", "nohup"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


_OPERATORS = ";&|()\n"
# A descriptor redirection (2>&1, >&2, &>file) is not a command separator; folded into one word before lexing.
_FD_REDIRECT = re.compile(r"(\d*[<>])&(\d+|-)|&>")


# Constructs this lexer cannot follow without misreading text as commands: a heredoc body, or a quoted argument made
# only of operator characters. A script containing either is not judged.
_UNJUDGED = re.compile(r"<<|(['\"])[;&|()\s]+\1")


def _without_redirections(words: list[str]) -> list[str]:
    """Drops each redirection operator (the lexer splits > and < from their descriptor), its descriptor number and
    its target, so neither a file name nor a descriptor is ever read as an argument."""
    kept: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word and set(word) <= set("<>"):
            if kept and kept[-1].isdigit():
                kept.pop()
            skip = True
        else:
            kept.append(word)
    return kept


def _simple_commands(script: str, depth: int = 0) -> list[list[str]]:
    """A script's simple commands as words, split only at unquoted shell operators: a quoted script handed to another
    program (docker run image sh -c '...') stays one word of that program's command. A nested sh -c script runs in
    the same process context, so its commands are judged too.

    The carrier rules built on this are deliberately conservative: they may miss a case (a command substitution, a
    comment, a selector such as -P "$PPID"), which execution then reports, but must never reject a valid command,
    since that would spend an author's corrections on nothing. A script the lexer cannot follow yields nothing.
    """
    if _UNJUDGED.search(script):
        return []
    script = _FD_REDIRECT.sub(lambda match: f"{match[1]}fd{match[2]}" if match[1] else ">both", script)
    lexer = shlex.shlex(script, posix=True, punctuation_chars=_OPERATORS)
    lexer.whitespace = " \t\r"
    commands: list[list[str]] = [[]]
    try:
        for token in lexer:
            if token and all(character in _OPERATORS for character in token):
                commands.append([])
            elif token not in {"{", "}"}:
                commands[-1].append(token)
    except ValueError:
        return []
    found: list[list[str]] = []
    for words in (_strip(_without_redirections(words)) for words in commands):
        if not words:
            continue
        found.append(words)
        program = _program(words)
        if depth < 2 and len(program) >= 3 and program[0].rsplit("/", 1)[-1] in _SHELLS and program[1] == "-c":
            found.extend(_simple_commands(program[2], depth + 1))
    return found


def _segments(command: Command) -> list[list[str]]:
    return _simple_commands(_script(command.argv))


def _program(words: list[str]) -> list[str]:
    """The words from the program on: leading variable assignments and wrappers such as sudo are dropped."""
    while words and (words[0] in _PREFIXES or _ASSIGNMENT.match(words[0])):
        words = words[1:]
    return words


def system_package_install(command: Command) -> bool:
    for words in _segments(command):
        words = _program(words)
        if words and (verbs := _PACKAGE_INSTALL.get(words[0].rsplit("/", 1)[-1])) and verbs & set(words[1:]):
            return True
    return False


# pkill and pgrep options that take a separate value, which is never the pattern.
_VALUED = frozenset({"-u", "-U", "-G", "-g", "-s", "-t", "-P", "--signal", "--euid", "--uid", "--group", "--pgroup",
                     "--session", "--terminal", "--parent", "--ns", "--nslist", "--pidfile", "-F"})
_REGEX_META = re.compile(r"[.*+?\[\](){}|\\$^]")


def _full_match(words: list[str], shell_line: str) -> bool:
    """pkill or pgrep matching the whole command line (-f, --full, or -f combined with other short flags) on a pattern
    that the script's own shell command line could match. Not judged: a parent-pid selector (-P), which the shell is
    not a child of; a bracket expression such as [b]in/tool, which cannot match its own literal text; and a ^-anchored
    pattern whose literal start differs from the start of the shell's command line."""
    if words[0].rsplit("/", 1)[-1] not in {"pkill", "pgrep"}:
        return False
    flags = [word for word in words[1:] if word.startswith("-")]
    full = any(flag == "--full" or (not flag.startswith("--") and "f" in flag[1:]) for flag in flags)
    if not full or any(flag == "-P" or flag.split("=", 1)[0] == "--parent" for flag in flags):
        return False
    operands, rest = [], iter(words[1:])
    for word in rest:
        if word in _VALUED:
            next(rest, None)
        elif not word.startswith("-"):
            operands.append(word)
    if not operands or "[" in (pattern := operands[-1]):
        return False
    if pattern.startswith("^"):
        literal = _REGEX_META.split(pattern[1:], maxsplit=1)[0]
        return not literal or shell_line.startswith(literal)
    return True


def self_matching_process_pattern(command: Command) -> bool:
    """A full-command-line pkill or pgrep inside a shell script that does more than that one command: the shell stays
    alive with the pattern in its own command line, so it matches (and pkill kills) itself. A shell running only the
    match replaces itself with it, and pkill and pgrep never match themselves."""
    if not (len(command.argv) == 3 and command.argv[0].rsplit("/", 1)[-1] in _SHELLS and command.argv[1] == "-c"):
        return False
    segments = _segments(command)
    shell_line = " ".join(command.argv)
    return len(segments) > 1 and any(_full_match(program, shell_line) for program in map(_program, segments) if program)


def validate_static_rules(install: Sequence[Command], activation: Activation, platform: str | None = None, *,
                          others: Sequence[Command] = (), system_packages: bool = True) -> None:
    """Raises the first correctable static error; each names its record ids (activation entries by position).

    ``others`` are the check and service lifecycle commands; ``system_packages`` is False for a carrier that cannot
    install system packages.
    """
    if not system_packages:
        for command in install:
            if system_package_install(command):
                raise _graph_error("system_package_install", "this carrier cannot install system packages: it has no "
                                   "Linux capabilities, so apt, dpkg, apk and dnf installs are refused; use a prebuilt "
                                   "release archive or extract packages under {cache}", command.id, set())
    for command in [*install, *others]:
        if self_matching_process_pattern(command):
            raise _graph_error("self_matching_process_pattern", "a full-command-line pkill or pgrep inside a longer shell "
                               "script matches the script's own shell; stop the process through a pid file written when "
                               "it starts", command.id, set())
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

