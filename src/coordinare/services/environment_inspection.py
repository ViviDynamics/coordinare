"""Generic paginated evidence navigation over one complete pinned inventory."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from coordinare_service_inference.environment.inventory import InventoryEntry
from coordinare_service_inference.environment.models import source_path

__all__ = ['InventoryEntry', 'SourceInspection']

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from coordinare.services.environment_discovery import DiscoveryLimits, Fetch


MAX_BATCH_FILES = 8


PolicyStatus = Literal['readable', 'too_large', 'link']


class SourceInspection:
    """One independent phase's exact read ledger; no ecosystem-specific navigation."""

    def __init__(self, paths: Sequence[str] | Mapping[str, InventoryEntry], fetch: Fetch, limits: DiscoveryLimits,
                 budget: AttemptBudget | None = None, *, retained_inputs: dict[str, str] | None = None) -> None:
        """A mapping inventory carries pinned sizes and modes; a plain path list keeps every path readable."""
        self.budget = budget
        self.retained_inputs = retained_inputs
        self.name = "discovery"
        self.paths = sorted(paths)
        self.entries = dict(paths) if isinstance(paths, Mapping) else None
        paths = list(paths)
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

    async def _read(self, path: str, *, navigation: bool = False) -> dict[str, str]:
        content = await self.fetch(path)
        if not isinstance(content, str):
            if navigation and (path in self.inputs or (self.retained_inputs is not None and path in self.retained_inputs)):
                message = 'previously inspected source is unavailable'
                raise EnvironmentDiscoveryError(message, code='source_identity')
            if navigation:
                return {'path': path, 'error': 'source_unavailable'}
            message = f"{self.name} source is unavailable or not text"
            raise EnvironmentDiscoveryError(message)
        return self.admit_content(path, content, fetched=True)

    def admit_content(self, path: str, content: str, *, fetched: bool) -> dict[str, str]:
        """Admit exact fetched or cached bytes under the same phase source bounds."""
        if path not in self.inventory:
            message = 'source content differs from inventory'
            raise EnvironmentDiscoveryError(message, code='source_identity')
        encoded = content.encode("utf-8")
        self.total_bytes += len(encoded)
        if fetched and self.budget is not None:
            self.budget.inspected_bytes += len(encoded)
        if len(encoded) > self.limits.max_file_bytes or self.total_bytes > self.limits.max_source_bytes:
            message = f"{self.name} source byte limit exceeded"
            raise EnvironmentDiscoveryError(message)
        digest = hashlib.sha256(encoded).hexdigest()
        if path in self.inputs and self.inputs[path] != digest:
            message = f"{self.name} source snapshot changed during inspection"
            raise EnvironmentDiscoveryError(message)
        if self.retained_inputs is not None:
            if self.retained_inputs.get(path, digest) != digest:
                message = 'retained source evidence changed'
                raise EnvironmentDiscoveryError(message, code='source_identity')
            self.retained_inputs[path] = digest
        self.inputs[path] = digest
        self.source_files[path] = content
        return {"path": path, "content": content, "sha256": digest}

    async def read_files(self, paths: Sequence[str], *, navigation: bool = False) -> dict[str, list[dict[str, str]]]:
        if (not isinstance(paths, list | tuple) or not 1 <= len(paths) <= MAX_BATCH_FILES
                or any(not isinstance(path, str) or path not in self.inventory for path in paths)
                or len(set(paths)) != len(paths)):
            message = f"{self.name} requested invalid source batch or paths"
            raise EnvironmentDiscoveryError(message, code="invalid_tool_arguments")
        if self.budget is not None:
            self.budget.reserve_reads(len(paths))
            await self.budget.flush()
        self.reads += len(paths)
        if self.reads > self.limits.max_reads:
            message = f"{self.name} file read limit exceeded"
            raise EnvironmentDiscoveryError(message)
        tasks = [asyncio.create_task(self._read(path, navigation=navigation)) for path in paths]
        try:
            return {"files": await asyncio.gather(*tasks)}
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.retained_inputs is not None and self.budget is not None:
                await self.budget.flush()

    def policy_status(self, path: str) -> PolicyStatus:
        """Symlinks and files over the per-file limit are listed but never read or followed."""
        entry = self.entries.get(path) if self.entries is not None else None
        if entry is None:
            return 'readable'
        if entry.mode == '120000':
            return 'link'
        return 'too_large' if entry.size > self.limits.max_file_bytes else 'readable'

    async def read(self, path: Any) -> dict[str, str]:
        return (await self.read_files([path]))["files"][0]

    async def _navigation_reads(self, paths: Any, *, single: bool = False) -> dict[str, Any]:
        """Recover missing model-selected paths; internal evidence reads remain strict."""
        if (not isinstance(paths, list) or not 1 <= len(paths) <= MAX_BATCH_FILES
                or any(not isinstance(path, str) for path in paths) or len(set(paths)) != len(paths)):
            message = "source batch must contain one to eight distinct string paths"
            raise EnvironmentDiscoveryError(message, code="invalid_tool_arguments")
        valid = [path for path in paths if path in self.inventory and self.policy_status(path) == 'readable']
        errors: list[dict[str, Any]] = [{"index": index, "code": "source_path_not_found" if path not in self.inventory
                                         else self.policy_status(path)}
                  for index, path in enumerate(paths) if path not in valid]
        result: dict[str, Any] = await self.read_files(valid, navigation=True) if valid else {"files": []}
        unavailable = [item for item in result['files'] if 'error' in item]
        errors.extend({'index': paths.index(item['path']), 'code': item['error']} for item in unavailable)
        result['files'] = [item for item in result['files'] if 'error' not in item]
        if not errors:
            return result["files"][0] if single else result
        result.update(errors=sorted(errors, key=lambda error: error['index']),
            guidance="Use list_files to select exact repository paths; root path is empty or '.'. "
            "Unavailable or non-text content, links (link) and files over the read limit (too_large) are listed but "
            "never read; they are not inspected evidence and cannot be cited. "
            "Inspect related repository declarations; unresolved required evidence must hold readiness.")
        return result

    async def tool_result(self, reply: dict[str, Any]) -> dict[str, Any]:
        if reply.get("tool") == "list_files" and set(reply) <= {"tool", "path", "cursor"}:
            path = reply.get("path", "")
            try:
                return self.list_directory("" if path == "." else path, reply.get("cursor", 0))
            except EnvironmentDiscoveryError as exc:
                if exc.code != "invalid_tool_arguments":
                    raise
                return {"errors": [{"code": "invalid_directory_or_cursor"}],
                        "guidance": "Use a listed directory path (root: empty or '.'); start at cursor 0, then use next_cursor."}
        if set(reply) == {"tool", "paths"} and reply.get("tool") == "read_files":
            return await self._navigation_reads(reply["paths"])
        if set(reply) == {"tool", "path"} and reply.get("tool") == "read_file":
            return await self._navigation_reads([reply["path"]], single=True)
        message = f"{self.name} returned an unsupported tool or envelope"
        raise EnvironmentDiscoveryError(message, code="unsupported_envelope")
