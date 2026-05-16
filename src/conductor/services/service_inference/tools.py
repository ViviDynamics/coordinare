"""Read-only sandboxed tools for the service-inference LLM agent (spec 063 T010).

All filesystem operations resolve paths against a project-root sandbox via
``realpath`` so that both ``..`` traversal and symlink escapes are rejected.
Tools never execute project code; ``probe_version`` only invokes binaries
resolved through ``shutil.which`` with a fixed ``--version``/``-v`` arg list.

Each public entry point returns a JSON-serialisable dict so the agent can feed
the result straight into the LLM conversation as a tool result.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SandboxViolation(ValueError):  # noqa: N818
    """Raised when a tool argument would escape the project sandbox."""


@dataclass(frozen=True)
class ToolSandbox:
    """Constrains every read tool to a single project root.

    ``root`` is resolved at construction time so the comparison always uses a
    canonical absolute path. ``web_search_enabled`` gates ``web_search``.
    ``grep_max_lines`` caps grep_repo output.
    """

    root: Path
    web_search_enabled: bool = False
    grep_max_lines: int = 100
    read_max_bytes: int = 64 * 1024
    list_max_entries: int = 200
    grep_max_files: int = 5000

    @classmethod
    def for_root(cls, root: Path | str, **kwargs: object) -> ToolSandbox:
        resolved = Path(root).resolve(strict=True)
        if not resolved.is_dir():
            msg = f"sandbox root must be a directory: {resolved}"
            raise SandboxViolation(msg)
        return cls(root=resolved, **kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ utils

    def _resolve_inside(self, rel_path: str) -> Path:
        """Resolve ``rel_path`` against ``root`` and reject any escape.

        Both ``..`` traversal and symlinks pointing outside the root are
        rejected because ``Path.resolve()`` collapses both into the same
        canonical form.
        """
        if not isinstance(rel_path, str) or not rel_path:
            raise SandboxViolation("path must be a non-empty string")
        # Reject absolute paths outright — they cannot be inside the relative
        # sandbox unless the caller already knew the root, which defeats the
        # purpose of the read tools.
        if Path(rel_path).is_absolute():
            raise SandboxViolation(f"absolute paths are not allowed: {rel_path!r}")
        candidate = self.root / rel_path
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SandboxViolation(f"cannot resolve {rel_path!r}: {exc}") from exc
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SandboxViolation(
                f"path escapes sandbox: {rel_path!r} -> {resolved}"
            ) from exc
        return resolved

    # ------------------------------------------------------------------ tools

    def read_file(self, path: str) -> dict[str, object]:
        target = self._resolve_inside(path)
        if not target.is_file():
            return {"path": path, "exists": False, "content": None}
        data = target.read_bytes()
        truncated = False
        if len(data) > self.read_max_bytes:
            data = data[: self.read_max_bytes]
            truncated = True
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "path": path,
                "exists": True,
                "binary": True,
                "size_bytes": target.stat().st_size,
                "truncated": truncated,
            }
        return {
            "path": path,
            "exists": True,
            "binary": False,
            "content": text,
            "truncated": truncated,
        }

    def list_dir(self, path: str = ".") -> dict[str, object]:
        target = self._resolve_inside(path)
        if not target.is_dir():
            return {"path": path, "exists": False, "entries": []}
        entries: list[dict[str, object]] = []
        for child in sorted(target.iterdir()):
            entries.append(
                {
                    "name": child.name,
                    "is_dir": child.is_dir(),
                    "is_file": child.is_file(),
                }
            )
            if len(entries) >= self.list_max_entries:
                break
        return {
            "path": path,
            "exists": True,
            "entries": entries,
            "truncated": len(entries) >= self.list_max_entries,
        }

    def which(self, binary: str) -> dict[str, object]:
        # No sandboxing on `which` — it queries PATH only; we still reject
        # whitespace/path separators so the agent can't ask "which /etc/passwd".
        if not isinstance(binary, str) or not binary or "/" in binary or any(
            c.isspace() for c in binary
        ):
            raise SandboxViolation(f"invalid binary name: {binary!r}")
        path = shutil.which(binary)
        return {"binary": binary, "path": path, "found": path is not None}

    def probe_version(self, binary: str) -> dict[str, object]:
        """Resolve ``binary`` via PATH then invoke ``--version`` / ``-v``.

        Refuses to run anything not first resolved by ``shutil.which`` so the
        agent cannot inject arbitrary paths. Stdout/stderr are merged and
        capped at 2 KiB.
        """
        info = self.which(binary)
        path = info["path"]
        if not path:
            return {"binary": binary, "found": False, "version": None}
        for flag in ("--version", "-v"):
            try:
                proc = subprocess.run(
                    [str(path), flag],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {
                    "binary": binary,
                    "found": True,
                    "version": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            combined = ((proc.stdout or "") + (proc.stderr or ""))[:2048].strip()
            if proc.returncode == 0 and combined:
                return {
                    "binary": binary,
                    "found": True,
                    "flag": flag,
                    "version": combined,
                }
        return {"binary": binary, "found": True, "version": None}

    def grep_repo(self, pattern: str, path: str = ".") -> dict[str, object]:
        """Search ``pattern`` within ``path`` (file or directory).

        Pure-Python so it works hermetically without depending on ripgrep being
        installed in the staging container. Capped at ``grep_max_lines``.
        """
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise SandboxViolation(f"invalid regex: {exc}") from exc
        target = self._resolve_inside(path)
        if not target.exists():
            return {"pattern": pattern, "matches": [], "truncated": False}
        files: list[Path] = []
        if target.is_file():
            files.append(target)
        else:
            # Walk explicitly so we can deduplicate by resolved inode and stop
            # symlink loops (rglob follows symlinks and will loop forever on
            # a self-referential tree). Cap total file count as a belt-and-braces.
            visited_dirs: set[Path] = set()
            stack: list[Path] = [target]
            while stack and len(files) < self.grep_max_files:
                current = stack.pop()
                try:
                    resolved_dir = current.resolve(strict=True)
                except OSError:
                    continue
                if resolved_dir in visited_dirs:
                    continue
                visited_dirs.add(resolved_dir)
                try:
                    resolved_dir.relative_to(self.root)
                except ValueError:
                    continue  # symlink pointing outside sandbox
                try:
                    children = list(current.iterdir())
                except OSError:
                    continue
                for child in children:
                    if child.is_symlink():
                        try:
                            child.resolve(strict=True).relative_to(self.root)
                        except (OSError, ValueError):
                            continue
                    if child.is_dir():
                        stack.append(child)
                    elif child.is_file():
                        files.append(child)
                        if len(files) >= self.grep_max_files:
                            break
        matches: list[dict[str, object]] = []
        truncated = False
        for f in files:
            try:
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, start=1):
                        if regex.search(line):
                            matches.append(
                                {
                                    "file": str(f.relative_to(self.root)),
                                    "line": lineno,
                                    "text": line.rstrip("\n")[:300],
                                }
                            )
                            if len(matches) >= self.grep_max_lines:
                                truncated = True
                                break
            except OSError:
                continue
            if truncated:
                break
        return {"pattern": pattern, "matches": matches, "truncated": truncated}

    def web_search(self, query: str) -> dict[str, object]:
        """Stub: gated behind ``web_search_enabled``.

        Phase 2 ships the gate; the actual search backend will land alongside
        the broader web-search integration. Today this returns a deterministic
        empty result so the agent path is exercisable end-to-end without
        depending on a network service.
        """
        if not isinstance(query, str) or not query.strip():
            raise SandboxViolation("query must be a non-empty string")
        if not self.web_search_enabled:
            return {"query": query, "enabled": False, "results": []}
        # TODO(spec-063 phase 4): wire a real backend when the project gains
        # one. Until then, returning [] is safer than hallucinating links.
        return {"query": query, "enabled": True, "results": []}
