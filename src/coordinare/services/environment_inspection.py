"""Generic paginated evidence navigation over one complete pinned inventory."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.models import source_path

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coordinare.services.environment_discovery import DiscoveryLimits, Fetch


MAX_BATCH_FILES = 8


class SourceInspection:
    """One independent phase's exact read ledger; no ecosystem-specific navigation."""

    def __init__(self, paths: Sequence[str], fetch: Fetch, limits: DiscoveryLimits, budget: AttemptBudget | None = None) -> None:
        self.budget = budget
        self.name = "discovery"
        self.paths = sorted(paths)
        self.inventory = set(paths)
        self.fetch = fetch
        self.limits = limits
        self.inputs: dict[str, str] = {}
        self.source_files: dict[str, str] = {}
        self.reads = 0
        self.total_bytes = 0
        self.inventory_digest = hashlib.sha256(json.dumps(self.paths, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self._directories: dict[str, dict[str, str]] = {"": {}}
        for path in self.paths:
            source_path(path)
            parts = path.split("/")
            for index, _part in enumerate(parts):
                parent = "/".join(parts[:index])
                child = "/".join(parts[:index + 1])
                kind = "file" if index == len(parts) - 1 else "directory"
                entries = self._directories.setdefault(parent, {})
                if child in entries and entries[child] != kind:
                    message = "source inventory contains overlapping file paths"
                    raise EnvironmentDiscoveryError(message)
                entries[child] = kind
                if kind == "directory":
                    self._directories.setdefault(child, {})
        self._entries = {path: [{"path": child, "kind": kind} for child, kind in sorted(children.items())]
                         for path, children in self._directories.items()}

    def list_directory(self, path: str = "", cursor: int = 0) -> dict[str, Any]:
        """Return a bounded page; the complete inventory never enters a tool result."""
        if not isinstance(path, str) or path not in self._entries or type(cursor) is not int:
            message = "source directory or cursor is invalid"
            raise EnvironmentDiscoveryError(message, code="invalid_tool_arguments")
        entries = self._entries[path]
        if cursor < 0 or cursor > len(entries):
            message = "source directory cursor is invalid"
            raise EnvironmentDiscoveryError(message, code="invalid_tool_arguments")
        result: dict[str, Any] = {"entries": [], "next_cursor": None}
        for entry in entries[cursor:cursor + 200]:
            result["entries"].append(entry)
            result["next_cursor"] = cursor + len(result["entries"])
            if len(json.dumps(result, ensure_ascii=False).encode()) > 32 * 1024:
                result["entries"].pop()
                if not result["entries"]:
                    message = "source directory entry exceeds page byte limit"
                    raise EnvironmentDiscoveryError(message)
                break
        end = cursor + len(result["entries"])
        result["next_cursor"] = end if end < len(entries) else None
        return result

    async def _read(self, path: str) -> dict[str, str]:
        content = await self.fetch(path)
        if not isinstance(content, str):
            message = f"{self.name} source is unavailable or not text"
            raise EnvironmentDiscoveryError(message)
        encoded = content.encode("utf-8")
        self.total_bytes += len(encoded)
        if self.budget is not None:
            self.budget.inspected_bytes += len(encoded)
        if len(encoded) > self.limits.max_file_bytes or self.total_bytes > self.limits.max_source_bytes:
            message = f"{self.name} source byte limit exceeded"
            raise EnvironmentDiscoveryError(message)
        digest = hashlib.sha256(encoded).hexdigest()
        if path in self.inputs and self.inputs[path] != digest:
            message = f"{self.name} source snapshot changed during inspection"
            raise EnvironmentDiscoveryError(message)
        self.inputs[path] = digest
        self.source_files[path] = content
        return {"path": path, "content": content, "sha256": digest}

    async def read_files(self, paths: Sequence[str]) -> dict[str, list[dict[str, str]]]:
        if (not isinstance(paths, list | tuple) or not 1 <= len(paths) <= MAX_BATCH_FILES
                or any(not isinstance(path, str) or path not in self.inventory for path in paths)
                or len(set(paths)) != len(paths)):
            message = f"{self.name} requested invalid source batch or paths"
            raise EnvironmentDiscoveryError(message, code="invalid_tool_arguments")
        if self.budget is not None:
            self.budget.reserve_reads(len(paths))
        self.reads += len(paths)
        if self.reads > self.limits.max_reads:
            message = f"{self.name} file read limit exceeded"
            raise EnvironmentDiscoveryError(message)
        tasks = [asyncio.create_task(self._read(path)) for path in paths]
        try:
            return {"files": await asyncio.gather(*tasks)}
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def read(self, path: Any) -> dict[str, str]:
        return (await self.read_files([path]))["files"][0]

    async def tool_result(self, reply: dict[str, Any]) -> dict[str, Any]:
        if reply.get("tool") == "list_files" and set(reply) <= {"tool", "path", "cursor"}:
            return self.list_directory(reply.get("path", ""), reply.get("cursor", 0))
        if set(reply) == {"tool", "paths"} and reply.get("tool") == "read_files":
            return await self.read_files(reply["paths"])
        if set(reply) == {"tool", "path"} and reply.get("tool") == "read_file":
            return await self.read(reply["path"])
        message = f"{self.name} returned an unsupported tool or envelope"
        raise EnvironmentDiscoveryError(message, code="unsupported_envelope")
