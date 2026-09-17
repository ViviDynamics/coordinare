"""Read-only command policy for the architect survey (spec 165 FR-004).

The model proposes commands; this module decides. It is deliberately a
whitelist of a few read-only programs (and a few read-only git subcommands),
not a blacklist: a live architect round ran ``bundle install``, wrote files
and slept, despite a persona that forbade all three. Anything not recognised
is refused with a reason, and the survey records the refusal.

Pure functions, no I/O, so every rule is a one-line test.
"""
from __future__ import annotations

import re
import shlex

_READ_ONLY_PROGRAMS: frozenset[str] = frozenset(
    {"ls", "cat", "head", "tail", "sed", "rg", "grep", "find", "wc", "sort", "uniq", "git", "tr", "cut"},
)
_GIT_READ_ONLY: frozenset[str] = frozenset(
    {"log", "show", "diff", "ls-files", "status", "blame", "rev-parse", "ls-tree", "branch"},
)
_FIND_FORBIDDEN: frozenset[str] = frozenset(
    {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"},
)
_SHELL_META = re.compile(r"[><`$]")  # redirections and substitutions
# The two stderr redirects a survey legitimately uses; live models attach them
# to most commands. Stripped before the meta scan, nothing else with `>` is.
_HARMLESS_REDIRECTS = re.compile(r"\s2>(/dev/null|&1)(?=\s|$)")
# sed: exactly one script, and it may only print a line range or a pattern match.
# No slash inside the pattern, so `/a/w /tmp/p` cannot pose as `/.../p`.
_SED_RANGE_PRINT = re.compile(r"^(\d+(,\d+)?|/[^/]*/)p$")  # `$` is already refused as shell meta
_SED_OPTIONS: frozenset[str] = frozenset({"-n", "-E", "-r", "--sandbox"})
# git: no global options before the subcommand (that is where -c, -C, --git-dir
# and --exec-path live), and no option that writes or runs a helper after it.
_GIT_FORBIDDEN_PREFIXES: tuple[str, ...] = ("--output", "--external-diff", "--ext-diff", "--textconv")
_GIT_BRANCH_WRITES: frozenset[str] = frozenset(
    {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u", "--delete", "--move", "--copy", "--force",
     "--set-upstream-to", "--unset-upstream", "--edit-description", "--track", "--no-track"},
)
# rg/grep: --pre runs a preprocessor per file, --hostname-bin runs a binary.
_GREP_FORBIDDEN_PREFIXES: tuple[str, ...] = ("--pre", "--hostname-bin")


def is_allowed(command: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for one shell line.

    ``;``, ``&&`` and ``|`` chains are allowed only when every segment is
    allowed on its own. Redirections and substitutions are refused outright,
    because they turn a read into a write.
    """
    text = (command or "").strip()
    if not text:
        return False, "empty command"
    text = _HARMLESS_REDIRECTS.sub("", text)
    if _SHELL_META.search(text):
        return False, "redirection or substitution is not read-only"
    try:
        segments = _split_chain(text)
    except ValueError as exc:
        return False, f"unparseable command: {exc}"
    for argv in segments:
        ok, reason = _segment_allowed(argv)
        if not ok:
            return False, reason
    return True, "read-only"


def _split_chain(text: str) -> list[list[str]]:
    """Split a shell line into per-command argv lists on ``|``, ``||``, ``&&``
    and ``;`` outside quotes and escapes, then let shlex parse each command.

    A ``|`` inside a grep pattern stays a pattern (a live round proposed
    ``grep 'a\\|b' f | head`` and a regex split cut it mid-quote) and an
    escaped ``\\(`` in a find expression stays an argument. A bare ``&``,
    ``(`` or ``)`` is a shell control token and makes the line unparseable.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i : i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(text[i : i + 2])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if text.startswith(("||", "&&"), i):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in "|;":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        if ch in "&()":
            raise ValueError(f"shell control token {ch!r}")
        buf.append(ch)
        i += 1
    if quote:
        raise ValueError("No closing quotation")
    segments.append("".join(buf))
    return [shlex.split(seg) for seg in segments]


def _segment_allowed(argv: list[str]) -> tuple[bool, str]:
    if not argv:
        return False, "empty segment in a chain"
    prog = argv[0].rsplit("/", 1)[-1]
    if prog not in _READ_ONLY_PROGRAMS:
        return False, f"{prog!r} is not a read-only program"
    rule = _PROGRAM_RULES.get(prog)
    return rule(argv[1:]) if rule else (True, "read-only")


def _git_rule(args: list[str]) -> tuple[bool, str]:
    if not args:
        return False, "git needs a read-only subcommand"
    if args[0].startswith("-"):
        return False, f"git global option {args[0]!r} is not allowed before the subcommand"
    sub, rest = args[0], args[1:]
    if sub not in _GIT_READ_ONLY:
        return False, f"git {sub!r} is not read-only"
    for a in rest:
        if a.startswith(_GIT_FORBIDDEN_PREFIXES):
            return False, f"git {sub} {a.split('=')[0]} writes a file or runs a helper"
    if sub == "branch":
        bad = [a for a in rest if a.split("=")[0] in _GIT_BRANCH_WRITES]
        if bad:
            return False, f"git branch {bad[0].split('=')[0]} writes"
        if any(not a.startswith("-") for a in rest):
            return False, "git branch with a branch name creates or edits a branch"
    return True, "read-only"


def _find_rule(args: list[str]) -> tuple[bool, str]:
    bad = _FIND_FORBIDDEN.intersection(args)
    if bad:
        return False, f"find {sorted(bad)[0]} executes, deletes or writes"
    return True, "read-only"


def _sed_rule(args: list[str]) -> tuple[bool, str]:
    usage = "sed is allowed only as `sed -n '<range>p' <file>`"
    options = [a for a in args if a.startswith("-")]
    if any(a.startswith("-i") for a in options):
        return False, "sed -i writes in place"
    if any(a not in _SED_OPTIONS for a in options):
        return False, usage
    if "-n" not in options:
        return False, usage
    positional = [a for a in args if not a.startswith("-")]
    if not positional or not _SED_RANGE_PRINT.match(positional[0]):
        return False, usage
    return True, "read-only"


def _grep_rule(args: list[str]) -> tuple[bool, str]:
    for a in args:
        if a.startswith(_GREP_FORBIDDEN_PREFIXES):
            return False, f"{a.split('=')[0]} runs a program"
    return True, "read-only"


def _sort_rule(args: list[str]) -> tuple[bool, str]:
    if any(a.startswith(("-o", "--output")) for a in args):
        return False, "sort -o writes a file"
    return True, "read-only"


def _uniq_rule(args: list[str]) -> tuple[bool, str]:
    if len([a for a in args if not a.startswith("-")]) > 1:
        return False, "uniq with a second file writes it"
    return True, "read-only"


_PROGRAM_RULES = {
    "git": _git_rule,
    "find": _find_rule,
    "sed": _sed_rule,
    "rg": _grep_rule,
    "grep": _grep_rule,
    "sort": _sort_rule,
    "uniq": _uniq_rule,
}
