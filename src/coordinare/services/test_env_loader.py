"""Coordinare-owned dotenv loader for symphony test-environment files (spec 092).

Given a validated :class:`~coordinare.config.TestEnvConfig` (or the agent-discovered
fallback path expressed as a repo-relative ``repo_path``), parse the named dotenv-style
file and return a plain ``dict[str, str]`` of literal values.

Hard invariants (carried from spec 091):

* **No shell execution** — no command substitution, no inter-variable interpolation.
  A value ``$(rm -rf /)`` is returned as the literal string ``"$(rm -rf /)"``.
* **Values are secret-like** — callers route them through the redacted ``secrets``
  channel. This module logs *nothing*: any future diagnostic must name only keys and
  the source, never values.
* **Containment is re-asserted at read time** — even though config-load already checked
  a ``repo_path`` for ``..`` / absoluteness, the loader rejects escapes again
  (defense in depth), because the fallback path and ``model_construct`` can bypass that.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinare.config import TestEnvConfig

__all__ = ["TestEnvFileError", "load_test_env", "parse_test_env"]


class TestEnvFileError(Exception):
    """A configured test-env file is missing, unreadable, or escapes the clone.

    The message names the resolved path and the originating field (``repo_path`` /
    ``host_path``) so the failure is actionable. It never carries file *contents*.
    """

    # Not a pytest test class despite the ``Test`` prefix (silences collection warning).
    __test__ = False


def _reassert_repo_containment(repo_path: str) -> None:
    """Reject a ``repo_path`` that is absolute or escapes the clone via ``..``.

    Mirrors the config-load rule on :class:`TestEnvConfig`; re-checked here because the
    agent-discovered fallback and ``model_construct`` can deliver an unvalidated value.
    """
    if PurePosixPath(repo_path).is_absolute() or Path(repo_path).is_absolute():
        msg = (
            f"test_env repo_path must be repo-relative, got absolute: {repo_path!r} "
            "(must stay inside the cloned symphony repo)"
        )
        raise TestEnvFileError(msg)
    if ".." in PurePosixPath(repo_path).parts:
        msg = (
            f"test_env repo_path escapes the clone (contains '..'): {repo_path!r}"
        )
        raise TestEnvFileError(msg)


def _resolve_source(cfg: TestEnvConfig, *, repo_root: Path) -> tuple[Path, str]:
    """Return ``(resolved_path, field_name)`` for the configured source."""
    if cfg.host_path is not None:
        return Path(cfg.host_path), "host_path"
    if cfg.repo_path is not None:
        _reassert_repo_containment(cfg.repo_path)
        return repo_root / cfg.repo_path, "repo_path"
    # Neither set: config-load forbids this, but guard defensively.
    msg = "test_env has neither repo_path nor host_path set"
    raise TestEnvFileError(msg)


def _strip_one_quote_layer(value: str) -> str:
    """Strip a single layer of matching surrounding quotes, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _parse_dotenv(text: str) -> dict[str, str]:
    """Parse dotenv-style text into a literal ``dict[str, str]`` (no shell semantics)."""
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        # Tolerate CRLF; ``splitlines`` keeps a trailing ``\r`` only on lone ``\r``,
        # but be explicit so embedded ``\r`` never leaks into a key/value.
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key:
            continue
        out[key] = _strip_one_quote_layer(value.strip())
    return out


def parse_test_env(text: str) -> dict[str, str]:
    """Parse dotenv-style ``text`` into literal ``KEY -> value`` pairs.

    The public, file-independent parse entry point. The coordinare has no on-disk
    clone of the symphony repo at env-bootstrap dispatch time, so a ``repo_path``
    source is fetched through the GitHub API and parsed here directly; ``load_test_env``
    handles the on-disk (``host_path`` / clone-relative) case.

    Same hard invariants as :func:`load_test_env`: no shell execution, no
    interpolation; values are taken literally.
    """
    return _parse_dotenv(text)


def load_test_env(cfg: TestEnvConfig, *, repo_root: Path) -> dict[str, str]:
    """Parse the configured test-env file and return literal ``KEY -> value`` pairs.

    Args:
        cfg: A ``TestEnvConfig`` naming exactly one of ``repo_path`` / ``host_path``.
        repo_root: The cloned symphony repo root (used to resolve ``repo_path``).

    Returns:
        A plain ``dict[str, str]``. An existing-but-empty file yields ``{}``.

    Raises:
        TestEnvFileError: The configured file is missing/unreadable, or a ``repo_path``
            escapes ``repo_root``. The message names the resolved path and the field.
    """
    resolved, field = _resolve_source(cfg, repo_root=repo_root)
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        msg = (
            f"test_env file not found: {resolved} "
            f"(configured via {field}); a configured test-env file must exist"
        )
        raise TestEnvFileError(msg) from exc
    except OSError as exc:
        msg = f"test_env file unreadable: {resolved} (configured via {field}): {exc}"
        raise TestEnvFileError(msg) from exc
    return _parse_dotenv(text)
