"""Unified diff parser for reviewer workflow (spec 169).

Parses a unified diff into ChangedFile structures with hunks and line ranges.
Detects truncation marker appended by _sanitize_pr_diff in dispatch_performer.py.
"""
from __future__ import annotations

import re

from performer.workflows.reviewer.models import ChangedFile, Hunk

__all__ = ["parse_unified_diff", "detect_truncation", "diff_lines", "line_in_hunks", "unread_beyond_truncation", "unread_names_from_note", "unread_overflow_from_note"]

#: The exact truncation note format written by dispatch_performer._sanitize_pr_diff
TRUNCATION_MARKER_PATTERN = r"\[coordinare: .*?diff truncated to \d+ chars.*?\]"


#: 412: "diff --git a/<path> b/<path>", including paths with spaces. Quoted
#: forms (git quotes paths with special characters) are unwrapped first; the
#: greedy first group then splits on the LAST " b/", which is the unquoted
#: git layout "a/<path> b/<path>". This is a FALLBACK: the exact target comes
#: from the "+++ b/<path>" line, which no " b/" inside the path can fool.
_GIT_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
#: 412 round 8: git quotes BOTH fields when either path needs quoting, and the
#: quoted form: the ``b/`` prefix is fixed, so no " b/" inside the path can
#: fool it, and the two quoted spans are parsed independently -- a quoted
#: RENAME has different source and target payloads (412 round 16). Delete-only
#: and mode-only sections have no ``+++ b/`` line, so the header is their only
#: name source.
_GIT_HEADER_QUOTED = re.compile(r'^diff --git "a/(?:[^"\\]|\\.)*" "b/((?:[^"\\]|\\.)*)"$')


def _decode_git_quoted_path(payload: str) -> str:
    """Decode git's quoted-path escapes into the real path.

    412 round 15: git quotes a header path it cannot emit raw (embedded
    quotes, non-ASCII under core.quotePath) and escapes the payload --
    ``\\"`` for a quote, ``\\\\`` for a backslash, ``\\t``/``\\n`` for real
    tab/newline characters, and one-to-three octal digits for a single
    UTF-8 byte. The escapes spell bytes, so decoding assembles UTF-8 from
    the octal runs rather than substituting Latin-1 characters.

    412 round 19: ``\\n``/``\\r`` stay escaped -- a decoded newline breaks
    the line-oriented machine metadata (one coordinare-cut/coordinare-unread
    line per path) and the parsed name can no longer be matched or reported
    exactly, which would let a truncated section read as fully covered.
    """
    data = bytearray()
    i = 0
    while i < len(payload):
        char = payload[i]
        if char != "\\" or i + 1 >= len(payload):
            data += char.encode("utf-8")
            i += 1
            continue
        escape = payload[i + 1 : i + 2]
        if escape in ('"', "\\"):
            data += escape.encode("utf-8")
            i += 2
        elif escape in ("n", "r"):
            # Line-structure characters stay escaped: the parsed name must
            # survive a round trip through single-line metadata verbatim.
            data += b"\\" + escape.encode("utf-8")
            i += 2
        elif escape == "t":
            data += b"\t"
            i += 2
        elif escape in ("a", "b", "v", "f"):
            # 412 round 24: git's C-style quoting covers the other control
            # bytes too -- the fallback branch would drop the backslash and
            # change the parsed filename.
            data += b"\a" if escape == "a" else b"\b" if escape == "b" else b"\v" if escape == "v" else b"\f"
            i += 2
        elif octal := re.match(r"[0-7]{1,3}", payload[i + 1 : i + 4]):
            data.append(int(octal.group(0), 8))
            i += 1 + len(octal.group(0))
        else:
            data += escape.encode("utf-8")
            i += 2
    return data.decode("utf-8", "replace")


def _path_from_header(line: str) -> str | None:
    """Extract the target path from "diff --git a/x b/x" (spaces tolerated)."""
    text = line.strip()
    quoted = _GIT_HEADER_QUOTED.match(text)
    if quoted:
        return _decode_git_quoted_path(quoted.group(1))
    text = text.replace('"', "")
    match = _GIT_HEADER.match(text)
    if not match:
        return None
    # 412 round 10: a mode-only section has no ---/+++ lines, so the header is
    # its only name source -- and the greedy split reduces a path containing
    # " b/" to its tail. Mode-only sides are the SAME path, so the exact form
    # is "a/<X> b/<X>": verify the two fields agree and take X.
    body = text[len("diff --git "):]
    if body.startswith("a/"):
        rest = body[2:]
        for i in range(len(rest) - 2):
            if rest[i:i + 3] == " b/" and rest[:i] == rest[i + 3:]:
                return rest[i + 3:]
    return match.group(2)


def parse_unified_diff(text: str) -> list[ChangedFile]:
    """Parse unified diff into ChangedFile structures with hunks.

    Args:
        text: The unified diff text, possibly with a truncation marker appended.

    Returns:
        List of ChangedFile objects with path, hunks, fully_in_diff, opened_by_survey.
    """
    changed_files: dict[str, ChangedFile] = {}
    current_file: str | None = None
    current_hunks: list[Hunk] = []
    current_deleted = False
    current_source = ""

    lines = text.split("\n")
    for line in lines:
        # Detect file header: "diff --git a/path b/path"
        if line.startswith("diff --git "):
            # Save the previous file if any
            if current_file is not None:
                changed_files[current_file] = ChangedFile(
                    path=current_file,
                    hunks=current_hunks,
                    # 412: hunks are the proof the diff carried this file's
                    # content. Rename-only, mode-change-only and delete-only
                    # entries carry none, so nothing was read from them.
                    fully_in_diff=bool(current_hunks),
                    opened_by_survey=False,
                    # 412 round 18: deletion is section metadata
                    # ("deleted file mode" / "+++ /dev/null"), not hunk
                    # presence -- an empty-file deletion has no hunks, and
                    # reading it as unread holds the round on nothing. The
                    # truncation handler clears the flag for sections that
                    # were actually cut.
                    deleted=current_deleted,
                )
            current_hunks = []
            current_deleted = False
            current_source = ""
            current_file = _path_from_header(line) or current_file
            continue

        if line.startswith("--- ") and not current_hunks and current_file is not None:
            # The a/-side metadata line -- the exact source path, unlike the
            # header's greedy split. Only read it outside hunks: inside a
            # hunk, "--- x" is a removed line.
            current_source = line[4:].strip()
            continue

        # 412: the "+++ b/<path>" line names the target exactly -- unlike the
        # header, no " b/" inside the path can fool it. Pure deletions carry
        # "+++ /dev/null"; keep the header-derived name for those. Only read
        # it outside hunks: inside a hunk, "+++ x" is an added line whose
        # content starts with "++".
        if line.startswith("+++ ") and not current_hunks and current_file is not None:
            rest = line[4:].strip()
            if rest == "/dev/null":
                # 412 round 8: a delete-only entry has no on-disk file to scan.
                current_deleted = True
                # 412 round 9: the header's greedy split mis-names an unquoted
                # delete-only path containing " b/"; the "--- a/<path>" line
                # is exact. "/dev/null" on both sides (created and deleted
                # within one range) keeps the header fallback.
                if current_source and current_source != "/dev/null":
                    # 412 round 18: the timestamp split happens before the
                    # decode here too (same reasoning as the +++ branch).
                    src = current_source.split("\t")[0].strip()
                    if src.startswith('"') and src.endswith('"'):
                        src = _decode_git_quoted_path(src[1:-1])
                    if src.startswith("a/"):
                        src = src[2:]
                    # 412 round 45: no trim after the decode -- the payload
                    # is byte-exact, and git quotes filenames with leading
                    # or trailing spaces precisely so they survive the
                    # header; metadata whitespace was trimmed pre-decode.
                    if src:
                        current_file = src
            else:
                # 412 round 18: a real tab separates an optional timestamp
                # suffix -- split it BEFORE decoding, because a quoted
                # payload's escaped tab only becomes a real tab during the
                # decode and must not read as a separator.
                rest = rest.split("\t")[0].strip()
                if rest.startswith('"') and rest.endswith('"'):
                    # 412 round 17: git escapes the quoted payload -- decode
                    # it rather than keeping the raw escape spelling.
                    rest = _decode_git_quoted_path(rest[1:-1])
                if rest.startswith("b/"):
                    rest = rest[2:]
                if rest:
                    current_file = rest
            continue

        # Detect rename or copy: "rename from/to" / "copy from/to" metadata
        if line.startswith("rename from ") or line.startswith("rename to ") or line.startswith("copy from ") or line.startswith("copy to "):
            # 412 round 5: the exact rename target. A rename-only section has
            # no "+++ b/" line to refine the header's greedy split, which
            # mis-splits a path containing " b/" into "bar.md".
            # 412 round 14: copy-only sections carry "copy to" instead.
            if (line.startswith("rename to ") or line.startswith("copy to ")) and current_file is not None and not current_hunks:
                # 412 round 17: a quoted metadata target is git-escaped;
                # decode it rather than stripping the quotes around the raw
                # escapes.
                target = line.split(" ", 2)[2].strip()
                if target.startswith('"') and target.endswith('"'):
                    target = _decode_git_quoted_path(target[1:-1])
                if target:
                    current_file = target
            continue

        # Detect new file: "new file mode"
        if line.startswith("new file mode"):
            continue

        # Detect deleted file: "deleted file mode"
        if line.startswith("deleted file mode"):
            continue

        # Detect hunk header: "@@ -a,b +c,d @@"
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if hunk_match:
            # 412 round 10: proof this section reached its content -- a
            # sanitizer cut before this line means the deletion's removals
            # were truncated, and the file must not read as covered.
            start_line = int(hunk_match.group(1))
            count = int(hunk_match.group(2)) if hunk_match.group(2) else 1

            # Skip hunks with no new-side lines (e.g., deleted files with +0,0)
            if count == 0:
                continue

            end_line = start_line + count - 1

            # Collect hunk lines until the next hunk or file header
            hunk_lines: list[str] = []
            current_hunks.append(
                Hunk(
                    header=line,
                    start_line=start_line,
                    end_line=end_line,
                    lines=hunk_lines,
                )
            )
            continue

        # Collect lines in the current hunk (context or added/removed)
        if current_hunks and (line.startswith("+") or line.startswith("-") or line.startswith(" ")):
            if current_hunks:
                current_hunks[-1].lines.append(line)

    # Save the last file
    if current_file is not None:
        changed_files[current_file] = ChangedFile(
            path=current_file,
            hunks=current_hunks,
            fully_in_diff=bool(current_hunks),
            opened_by_survey=False,
            # 412 round 18: deletion is section metadata, not hunk presence.
            deleted=current_deleted,
        )

    return list(changed_files.values())


def detect_truncation(text: str) -> bool:
    """Detect whether the diff was truncated by the sanitizer.

    Returns True if the text contains the truncation marker pattern.

    Args:
        text: The diff text, possibly with a truncation marker appended.

    Returns:
        True if truncation marker is present.
    """
    return bool(re.search(TRUNCATION_MARKER_PATTERN, text, re.DOTALL))


def diff_lines(text: str) -> list[str]:
    """Extract all added and context lines from the diff.

    Returns lines without the +/space prefix, for evidence matching.

    Args:
        text: The unified diff text.

    Returns:
        List of lines (added and context, with leading +/space removed).
    """
    result = []
    for line in text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            result.append(line[1:])
        elif line.startswith(" "):
            result.append(line[1:])
    return result


def line_in_hunks(file: ChangedFile, line: int) -> bool:
    """Check if a line number falls within any hunk's new-side range.

    Args:
        file: The ChangedFile object with hunks.
        line: The new-side line number to check.

    Returns:
        True if the line is in any hunk's range [start_line, end_line].
    """
    for hunk in file.hunks:
        if hunk.start_line <= line <= hunk.end_line:
            return True
    return False

#: 412: the sanitizer writes one machine line per unread file, after the
#: prose note. Rest-of-line is the exact path -- paths may contain commas,
#: semicolons or " b/", so prose lists (", ".join) were ambiguous. Column 0
#: exactly: a hunk's context line " coordinare-unread: x" is file content,
#: not metadata.
_UNREAD_LINE_PATTERN = re.compile(r"^coordinare-unread: (.+)$")
_CUT_LINE_PATTERN = re.compile(r"^coordinare-cut: (.+)$")


def unread_names_from_note(diff_text: str) -> list[str]:
    """File names the sanitizer's truncation note declares unread (412)."""
    names: list[str] = []
    for line in diff_text.split("\n"):
        match = _UNREAD_LINE_PATTERN.match(line) or _CUT_LINE_PATTERN.match(line)
        if match:
            # 412 round 24: the capture is verbatim -- the sanitizer emits the
            # path exactly, and .strip() would corrupt a valid name with
            # leading/trailing spaces until it no longer matches the
            # ChangedFile the coverage gates hold on.
            name = match.group(1)
            if name:
                names.append(name)
    return names


#: 412 round 6: the sanitizer bounds the per-path metadata; paths beyond the
#: budget are declared by count, and an unknown-size unread set is a hold.
_OVERFLOW_LINE_PATTERN = re.compile(r"^coordinare-unread-overflow: (\d+)$")

#: 412 round 12: the bare-note fallback's phantom -- unopenable by
#: construction, so the coverage gates hold on the unnamed remainder of a
#: truncated diff whose note named nothing.
_UNNAMED_TAIL_PATH = "<unnamed files beyond the truncated diff>"


def unread_overflow_from_note(diff_text: str) -> int:
    """How many unread paths the note could not name (0 when it named all)."""
    for line in diff_text.split("\n"):
        match = _OVERFLOW_LINE_PATTERN.match(line)
        if match:
            return int(match.group(1))
    return 0


def unread_beyond_truncation(files: list[ChangedFile], diff_text: str) -> list[ChangedFile]:
    """412: hold unread what the truncation cut.

    Named files already parsed lose ``fully_in_diff``; named files entirely
    beyond the cap enter as phantom changed files with nothing read, so
    coverage holds them unread instead of passing on the visible subset.
    """
    if not files:
        named_only = unread_names_from_note(diff_text)
        # 412: truncation before the first ``diff --git`` header parses to
        # nothing, but the note still names every omitted file -- phantoms, so
        # coverage holds them unread instead of reading an outage as empty.
        return [ChangedFile(path=n, hunks=[], fully_in_diff=False, opened_by_survey=False) for n in named_only]
    named = unread_names_from_note(diff_text)
    if not named:
        # Fallback for a bare note: the sanitizer cuts the tail, so the last
        # file is the one it cut through.
        # 412 round 11: a cut-through file parsed as a deletion keeps
        # ``deleted`` from its hunk header, and the coverage gates exempt
        # deletions -- so the cut must clear that exemption too, or the
        # truncated removal body passes as covered.
        out = list(files)
        # 412 round 20: ``deleted_before_cut`` preserves the hunk-header
        # truth -- the argv filter needs it (a cut-through deletion is not
        # on disk) even though ``deleted`` itself is cleared for coverage.
        out[-1] = out[-1].model_copy(
            update={"fully_in_diff": False, "deleted": False, "deleted_before_cut": out[-1].deleted}
        )
        # 412 round 12: a bare note gives no trustworthy account of the hidden
        # tail -- append an unopenable phantom so the coverage gates hold on
        # the unnamed remainder instead of passing on the visible subset once
        # the cut-through file itself is opened.
        out.append(
            ChangedFile(path=_UNNAMED_TAIL_PATH, hunks=[], fully_in_diff=False, opened_by_survey=False)
        )
        return out
    parsed = {f.path for f in files}
    out = [
        f.model_copy(update={"fully_in_diff": False, "deleted": False, "deleted_before_cut": f.deleted})
        if f.path in parsed and f.path in set(named)
        else f
        for f in files
    ]
    out.extend(ChangedFile(path=n, hunks=[], fully_in_diff=False, opened_by_survey=False) for n in named if n not in parsed)
    return out
