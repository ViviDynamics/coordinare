"""Mechanical degeneracy guard for text artifacts (issue 396).

An agent turn can degenerate into single-token repetition — card #160's
architect emitted 115,038 lines / 534 KB whose body was ``No.`` x37,738 and
``Go.`` x37,738 — and nothing between "the agent wrote a file" and "the file
is committed and pushed" looked at what the file contains, so the artifact
landed on an open PR as ``plan.md``.

This module is the cheap check the issue asks for: pure, synchronous, no model
call and no tech-stack knowledge (#364). The signatures are exact-line
repetition (the incident's ``No.``/``Go.`` body) and an out-of-band byte size
for documentation artifacts. Thresholds are deliberately conservative: the
repetition rules only engage at 500+ non-empty lines, so no honest document
under a few hundred lines is ever examined for shape.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DegenerateArtifactError",
    "DegeneracyVerdict",
    "classify_bytes",
    "classify_file",
    "classify_text",
    "format_refusal",
    "HARD_READ_LIMIT",
]

#: A text artifact past this size is wildly out of band. The incident's
#: plan.md was 534 KB against ~190 lines of real feature work.
DEFAULT_SIZE_CAP = 262_144

#: The most common line may not own more than this share of a large text file.
DEFAULT_TOP_LINE_FRACTION = 0.25

#: Repetition rules only engage at this many non-empty lines. Below it, files
#: are never examined for shape: short documents legitimately repeat lines.
DEFAULT_MIN_REPETITION_LINES = 500

#: A large file whose non-empty lines are at most this fraction unique has
#: stopped carrying information (the incident body: 5 distinct lines in 115k).
DEFAULT_UNIQUE_LINE_RATIO_MAX = 0.05

#: Above this size a file is never read into memory; when a size cap applies
#: the size alone is disqualifying.
HARD_READ_LIMIT = 64 * 1024 * 1024

# Streaming full-file scan (oversized files with the size rule off).
_STREAM_CHUNK = 1 << 20
_MAX_TRACKED_UNIQUE = 100_000


class DegenerateArtifactError(RuntimeError):
    """Raised when an artifact is refused before commit.

    Subclasses :class:`RuntimeError` so existing broad ``except Exception``
    handlers (assessor, security) treat it like any other commit failure.
    """

    def __init__(self, path: str, reasons: tuple[str, ...]) -> None:
        super().__init__(f"degenerate artifact refused: {path}: {'; '.join(reasons)}")
        self.path = path
        self.reasons = reasons


@dataclass(frozen=True)
class DegeneracyVerdict:
    """The outcome of the mechanical degeneracy check on one text artifact."""

    degenerate: bool
    reasons: tuple[str, ...] = ()


def _size_reason(size: int, cap: int) -> str:
    return f"file is {size} bytes, over the {cap} byte cap for a text artifact"


def classify_text(
    content: str,
    *,
    size_cap: int | None = DEFAULT_SIZE_CAP,
    top_line_fraction: float = DEFAULT_TOP_LINE_FRACTION,
    min_repetition_lines: int = DEFAULT_MIN_REPETITION_LINES,
    unique_line_ratio_max: float = DEFAULT_UNIQUE_LINE_RATIO_MAX,
) -> DegeneracyVerdict:
    """Classify *content* by exact-line repetition and size. Pure.

    ``size_cap=None`` disables the size rule (used by the tree sweep, where
    legitimate source files can legitimately be large); the repetition rules
    stay on. Every threshold is a keyword argument so tests and callers can
    tune without subclassing.
    """
    reasons: list[str] = []
    encoded = content.encode("utf-8")
    if size_cap is not None and len(encoded) > size_cap:
        reasons.append(_size_reason(len(encoded), size_cap))

    lines = [line for line in content.split("\n") if line.strip()]
    if len(lines) >= min_repetition_lines:
        counts: dict[str, int] = {}
        for line in lines:
            counts[line] = counts.get(line, 0) + 1
        top_line, top_count = max(counts.items(), key=lambda item: item[1])
        top_share = top_count / len(lines)
        unique_ratio = len(counts) / len(lines)
        if top_share >= top_line_fraction:
            # The repeated line itself is never reported verbatim: a repeated
            # source/config line can carry a credential, and these reasons go
            # to logs and turn failures. A digest is enough to correlate.
            digest = hashlib.sha256(top_line.encode("utf-8")).hexdigest()[:16]
            reasons.append(
                f"repetition: one line (sha256:{digest}, {len(top_line)} chars) "
                f"occurs {top_count} times of {len(lines)} non-empty lines "
                f"({top_share:.0%})"
            )
        if unique_ratio <= unique_line_ratio_max:
            reasons.append(
                f"repetition: only {len(counts)} unique lines among "
                f"{len(lines)} non-empty lines ({unique_ratio:.1%} unique)"
            )

    return DegeneracyVerdict(degenerate=bool(reasons), reasons=tuple(reasons))


def classify_file(path: Path, **kwargs: object) -> DegeneracyVerdict:
    """Classify the file at *path*, skipping missing/binary files.

    Never raises for an unreadable or undecodable file: a binary is not a
    text artifact, and a missing path has nothing to classify. Keyword
    arguments pass through to :func:`classify_text`.
    """
    try:
        stat = path.stat()
        if not stat.st_size:
            return DegeneracyVerdict(degenerate=False)
        cap = kwargs.get("size_cap") if "size_cap" in kwargs else DEFAULT_SIZE_CAP
        # One read for every non-streamed path: sniffing and decoding share it.
        with path.open("rb") as handle:
            data = handle.read(min(stat.st_size, HARD_READ_LIMIT))
        if b"\x00" in data:
            # Binary before any size verdict: an oversized asset is skipped,
            # not classified as a degenerate text artifact.
            return DegeneracyVerdict(degenerate=False)
        oversize = stat.st_size > HARD_READ_LIMIT
        over_cap = cap is not None and stat.st_size > int(cap)
        if over_cap:
            # The cap applies at any size: the size verdict is independent of
            # the hard streaming boundary (#396).
            return DegeneracyVerdict(
                degenerate=True, reasons=(_size_reason(stat.st_size, int(cap)),)
            )
        if oversize:
            # With the size rule off, never buffer the file whole: stream it
            # with a bounded per-line counter so the repetition rules see the
            # entire file instead of a leading prefix (#396).
            return _streaming_verdict(path, **kwargs)
        content = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return DegeneracyVerdict(degenerate=False)
    return classify_text(content, **kwargs)  # type: ignore[arg-type]


class _StreamScanner:
    """Incremental exact-line counter for the streaming degeneracy scan.

    Lines are keyed by the sha256 of their raw bytes (split on ``\\n``,
    whitespace-only lines skipped) exactly as :func:`classify_text` counts
    them, but nothing is retained except the bounded per-line counter: the
    hash state for the line in progress is O(1), so a newline-free file
    cannot grow a buffer (#396).

    When the counter fills past ``_MAX_TRACKED_UNIQUE`` distinct lines, a
    Misra-Gries-style decimation evicts entries at or below a quarter of the
    current maximum count, so a line occupying more than a quarter of the
    file survives eviction and keeps counting and the top-line rule stays
    correct after overflow. The exact unique-ratio rule is skipped on
    overflow instead of mis-evaluated.
    """

    def __init__(
        self,
        *,
        top_line_fraction: float,
        min_repetition_lines: int,
        unique_line_ratio_max: float,
    ) -> None:
        self._top_line_fraction = top_line_fraction
        self._min_repetition_lines = min_repetition_lines
        self._unique_line_ratio_max = unique_line_ratio_max
        self._counts: dict[bytes, int] = {}
        self._lengths: dict[bytes, int] = {}
        self._overflow = False
        self._total = 0
        self._hasher: "hashlib._Hash" = hashlib.sha256()
        self._line_bytes = 0
        self._line_nonspace = False

    def feed(self, chunk: bytes) -> None:
        """Absorb one chunk of bytes; lines may span chunk boundaries."""
        pieces = chunk.split(b"\n")
        for piece in pieces[:-1]:
            self._advance(piece, final=True)
        self._advance(pieces[-1], final=False)

    def _advance(self, piece: bytes, *, final: bool) -> None:
        self._hasher.update(piece)
        self._line_bytes += len(piece)
        if not self._line_nonspace and piece.strip():
            self._line_nonspace = True
        if final:
            self._end_line()

    def _end_line(self) -> None:
        digest = self._hasher.digest()
        raw_len = self._line_bytes
        has_nonspace = self._line_nonspace
        self._hasher = hashlib.sha256()
        self._line_bytes = 0
        self._line_nonspace = False
        if not has_nonspace:
            return
        self._total += 1
        if digest in self._counts:
            self._counts[digest] += 1
            return
        if len(self._counts) < _MAX_TRACKED_UNIQUE:
            self._counts[digest] = 1
            self._lengths[digest] = raw_len
            return
        self._overflow = True
        self._decimate()
        if len(self._counts) < _MAX_TRACKED_UNIQUE:
            self._counts[digest] = 1
            self._lengths[digest] = raw_len

    def _decimate(self) -> None:
        """Evict the light lines so a heavy hitter can still be counted."""
        if not self._counts:
            return
        threshold = max(1, max(self._counts.values()) // 4)
        light = [k for k, v in self._counts.items() if v <= threshold]
        if not light:
            floor = min(self._counts.values())
            light = [k for k, v in self._counts.items() if v <= floor]
        for k in light:
            del self._counts[k]
            self._lengths.pop(k, None)

    def finish(self) -> DegeneracyVerdict:
        """Close the stream and classify everything fed so far."""
        self._end_line()
        if self._total < self._min_repetition_lines:
            return DegeneracyVerdict(degenerate=False)
        reasons: list[str] = []
        top_key, top_count = max(self._counts.items(), key=lambda item: item[1])
        top_share = top_count / self._total
        if top_share >= self._top_line_fraction:
            digest = top_key.hex()[:16]
            reasons.append(
                f"repetition: one line (sha256:{digest}, "
                f"{self._lengths[top_key]} chars) "
                f"occurs {top_count} times of {self._total} non-empty lines "
                f"({top_share:.0%})"
            )
        if not self._overflow:
            unique_ratio = len(self._counts) / self._total
            if unique_ratio <= self._unique_line_ratio_max:
                reasons.append(
                    f"repetition: only {len(self._counts)} unique lines among "
                    f"{self._total} non-empty lines ({unique_ratio:.1%} unique)"
                )
        return DegeneracyVerdict(degenerate=bool(reasons), reasons=tuple(reasons))


def stream_scanner(**kwargs: object) -> _StreamScanner:
    """Build a :class:`_StreamScanner` with :func:`classify_text` thresholds.

    Accepts the same keyword arguments as :func:`classify_text`;
    ``size_cap`` is ignored here (streaming callers enforce it separately).
    """
    return _StreamScanner(
        top_line_fraction=(
            float(kwargs["top_line_fraction"])
            if "top_line_fraction" in kwargs else DEFAULT_TOP_LINE_FRACTION
        ),
        min_repetition_lines=(
            int(kwargs["min_repetition_lines"])
            if "min_repetition_lines" in kwargs
            else DEFAULT_MIN_REPETITION_LINES
        ),
        unique_line_ratio_max=(
            float(kwargs["unique_line_ratio_max"])
            if "unique_line_ratio_max" in kwargs
            else DEFAULT_UNIQUE_LINE_RATIO_MAX
        ),
    )


def _streaming_verdict(path: Path, **kwargs: object) -> DegeneracyVerdict:
    """Bounded-memory full scan of a file too large to buffer whole (#396)."""
    scanner = stream_scanner(**kwargs)
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_STREAM_CHUNK):
                scanner.feed(chunk)
    except OSError:
        return DegeneracyVerdict(degenerate=False)
    return scanner.finish()


def classify_bytes(size: int, prefix: bytes, **kwargs: object) -> DegeneracyVerdict:
    """Classify a blob of *size* bytes from its leading *prefix*.

    Used where the bytes of interest are an index blob rather than a worktree
    file: the staged version can differ from what is on disk (#396). The
    caller bounds the prefix at :data:`HARD_READ_LIMIT` bytes — a multi-
    gigabyte blob is classified from its leading slice, never buffered whole.
    """
    if size <= 0:
        return DegeneracyVerdict(degenerate=False)
    if b"\x00" in prefix:
        return DegeneracyVerdict(degenerate=False)
    cap = kwargs.get("size_cap") if "size_cap" in kwargs else DEFAULT_SIZE_CAP
    if size > HARD_READ_LIMIT and cap is not None and size > int(cap):
        return DegeneracyVerdict(
            degenerate=True, reasons=(_size_reason(size, int(cap)),)
        )
    content = prefix.decode("utf-8", errors="ignore")
    return classify_text(content, **kwargs)  # type: ignore[arg-type]


def format_refusal(path: str, verdict: DegeneracyVerdict) -> str:
    """One-line refusal reason for logs and turn failures."""
    return f"{path}: {'; '.join(verdict.reasons)}"
