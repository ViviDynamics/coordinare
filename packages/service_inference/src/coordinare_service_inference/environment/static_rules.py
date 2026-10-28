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


# Constructs this lexer cannot follow without misreading text as commands: a heredoc body, a quoted argument made
# only of operator characters, or an escaped operator. A script containing any of them is not judged.
_UNJUDGED = re.compile(r"<<|(['\"])[;&|()\s]+\1|\\[;&|()]")


_REDIRECTION = re.compile(r"\d*[<>]+")


def _without_redirections(words: list[str]) -> list[str]:
    """Drops each redirection, whether its target is attached (>/dev/null, 2>fd1) or the next word (> log), so neither
    a file name nor a descriptor is ever read as an argument."""
    kept: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif _REDIRECTION.fullmatch(word):
            skip = True
        elif not _REDIRECTION.match(word):
            kept.append(word)
    return kept


def _simple_commands(script: str, depth: int = 0, *, nested: bool = True) -> list[list[str]]:
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
    # Words break only at whitespace and unquoted operators, as in the shell: never at ':', '{' or '='.
    lexer.whitespace_split = True
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
        if nested and depth < 2 and len(program) >= 3 and program[0].rsplit("/", 1)[-1] in _SHELLS and program[1] == "-c":
            found.extend(_simple_commands(program[2], depth + 1))
    return found


def _segments(command: Command) -> list[list[str]]:
    return _simple_commands(_script(command.argv))


def _program(words: list[str]) -> list[str]:
    """The words from the program on: leading variable assignments and wrappers such as sudo are dropped."""
    while words and (words[0] in _PREFIXES or _ASSIGNMENT.match(words[0])):
        words = words[1:]
    return words


# Requests that only simulate, download or describe an install, and need no installation capability.
_NO_INSTALL = frozenset({"-s", "--simulate", "--just-print", "--dry-run", "--recon", "--no-act", "--help", "-h",
                         "-d", "--download-only", "--print-uris", "--downloadonly"})
# Options that may precede the subcommand without taking a separate value; any other leaves the command unjudged,
# since its value could be mistaken for the subcommand.
_SELF_CONTAINED = re.compile(r"-[yq]+|--[a-z][a-z-]*=.*|--(yes|assume-yes|quiet|no-cache|no-install-recommends|"
                             r"no-install-suggests|no-progress|force-yes|allow-unauthenticated)")


def _no_install(word: str) -> bool:
    """A simulate, download-only or help request, also as --name=value or inside a short cluster such as -dy."""
    return (word.split("=", 1)[0] in _NO_INSTALL
            or (re.fullmatch(r"-[A-Za-z]{2,}", word) is not None and bool(set(word[1:]) & {"d", "s", "h"})))


def system_package_install(command: Command) -> bool:
    """A package manager whose subcommand (its first non-option word) installs, or dpkg -i: an option before the
    subcommand that takes a separate value hides it, which is an accepted miss."""
    for words in _segments(command):
        words = _program(words)
        if not words or (verbs := _PACKAGE_INSTALL.get(words[0].rsplit("/", 1)[-1])) is None:
            continue
        arguments = words[1:]
        if any(_no_install(word) for word in arguments):
            continue
        if words[0].rsplit("/", 1)[-1] == "dpkg":
            if verbs & set(arguments):
                return True
        else:
            index = next((position for position, word in enumerate(arguments) if not word.startswith("-")), None)
            if (index is not None and arguments[index] in verbs
                    and all(_SELF_CONTAINED.fullmatch(word) for word in arguments[:index])):
                return True
    return False


# A full-command-line match is judged only in its plainest form: exactly -f or --full, otherwise at most a signal,
# on a literal pattern. A literal appears verbatim in the script's own command line, so it certainly matches the
# shell; any other option or any regular-expression syntax is not judged.
_SIGNAL = re.compile(r"-(\d+|[A-Z][A-Z0-9+-]*)")
_LITERAL = re.compile(r"[A-Za-z0-9_./:@%=,~ -]+")


def _certain_self_match(words: list[str], script: str) -> bool:
    if words[0].rsplit("/", 1)[-1] not in {"pkill", "pgrep"}:
        return False
    operands: list[str] = []
    full = False
    rest = iter(words[1:])
    for word in rest:
        if word in {"-f", "--full"}:
            full = True
        elif word == "--signal":
            next(rest, None)
        elif word.startswith("--signal=") or _SIGNAL.fullmatch(word):
            continue
        elif word.startswith("-"):
            return False
        else:
            operands.append(word)
    # The operand must also appear verbatim in the script text: quoting or escaping ('w''x', foo\\ bar) changes what
    # is searched for without changing the shell's own command line.
    return full and len(operands) == 1 and bool(_LITERAL.fullmatch(operands[0])) and operands[0] in script


def self_matching_process_pattern(command: Command) -> bool:
    """A full-command-line pkill or pgrep inside a shell script that does more than that one command: the shell stays
    alive with the pattern in its own command line, so it matches (and pkill kills) itself. A shell running only the
    match replaces itself with it, as exec does, and pkill and pgrep never match themselves."""
    if not (len(command.argv) == 3 and command.argv[0].rsplit("/", 1)[-1] in _SHELLS and command.argv[1] == "-c"):
        return False
    # Only the script's own top-level commands: a nested shell has its own command line.
    script = command.argv[2]
    segments = _simple_commands(script, nested=False)
    # A pipeline can filter the shell's own pid out of pgrep's output (pgrep -f x | grep -vx "$$"); pkill has
    # already killed by then, so only pgrep is left unjudged.
    piped = re.search(r"(?<!\|)\|(?!\|)", script) is not None
    return len(segments) > 1 and any(
        _certain_self_match(program, script) for words in segments
        if (program := _program(words)) and "exec" not in words[:len(words) - len(program)]
        and not (piped and program[0].rsplit("/", 1)[-1] == "pgrep"))


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

