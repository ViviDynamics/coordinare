"""Size-based source shards planned only from pinned paths, sizes and modes."""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from coordinare_service_inference.environment.inventory import InventoryEntry

BYTES_PER_TOKEN = 3.5
CONTEXT_SHARE = 0.5
# A listed file also costs its path and JSON framing in a conversation.
PATH_OVERHEAD = 96


@dataclass(frozen=True)
class Shard:
    id: str
    paths: tuple[str, ...]
    readable_bytes: int
    depth: int


def share_bytes(context_tokens: int) -> int:
    """About half a model context, so instructions, evidence and replies still fit."""
    return int(context_tokens * CONTEXT_SHARE * BYTES_PER_TOKEN)


def readable(entry: InventoryEntry, *, max_file_bytes: int) -> bool:
    return bool(entry.mode != '120000' and entry.size <= max_file_bytes)


def shard_weight(entry: InventoryEntry) -> int:
    return int(entry.size) + len(entry.path) + PATH_OVERHEAD


def spine_paths(entries: Mapping[str, InventoryEntry], *, limit_bytes: int, max_file_bytes: int) -> list[str]:
    """Readable root-level files, smallest first, that every shard agent receives."""
    root = sorted((entry for entry in entries.values() if '/' not in entry.path and readable(entry, max_file_bytes=max_file_bytes)),
                  key=lambda entry: (entry.size, entry.path))
    totals = itertools.accumulate(shard_weight(entry) for entry in root)
    return sorted(entry.path for entry, total in zip(root, totals, strict=True) if total <= limit_bytes)


def _units(directory: str, files: dict[str, list[InventoryEntry]], dirs: dict[str, list[str]],
           totals: dict[str, int], share: int) -> list[tuple[list[str], int, int]]:
    """Each unit fits the share: a whole directory, or the split units of an oversized one."""
    depth = directory.count('/') + 1 if directory else 0
    children: list[tuple[str, list[tuple[list[str], int, int]]]] = [
        (entry.path, [([entry.path], shard_weight(entry), depth)]) for entry in files.get(directory, [])]
    for child in dirs.get(directory, []):
        if totals[child] <= share:
            children.append((child, [(_collect(child, files, dirs), totals[child], child.count('/') + 1)]))
        else:
            children.append((child, _units(child, files, dirs, totals, share)))
    return [unit for _name, units in sorted(children, key=lambda item: item[0]) for unit in units]


def _collect(directory: str, files: dict[str, list[InventoryEntry]], dirs: dict[str, list[str]]) -> list[str]:
    paths = [entry.path for entry in files.get(directory, [])]
    for child in dirs.get(directory, []):
        paths.extend(_collect(child, files, dirs))
    return sorted(paths)


def plan_shards(entries: Mapping[str, InventoryEntry], *, share: int, spine: Collection[str],
                max_file_bytes: int) -> list[Shard]:
    """Recursive, deterministic split: fitting directories stay whole, small neighbours pack together.

    A unit over half the share stands alone, so one large file never crowds out its siblings.
    """
    excluded = set(spine)
    files: dict[str, list[InventoryEntry]] = {}
    dirs: dict[str, set[str]] = {}
    totals: dict[str, int] = {}
    for path in sorted(entries):
        entry = entries[path]
        if path in excluded or not readable(entry, max_file_bytes=max_file_bytes):
            continue
        if shard_weight(entry) > share:
            message = 'shard share must hold every readable file'
            raise ValueError(message)
        parts = path.split('/')
        parent = '/'.join(parts[:-1])
        files.setdefault(parent, []).append(entry)
        for index in range(len(parts) - 1):
            ancestor = '/'.join(parts[:index + 1])
            dirs.setdefault('/'.join(parts[:index]), set()).add(ancestor)
            totals[ancestor] = totals.get(ancestor, 0) + shard_weight(entry)
    ordered = {key: sorted(value) for key, value in dirs.items()}
    bins: list[tuple[list[str], int, int]] = []
    for paths, weight, depth in _units('', files, ordered, totals, share):
        last = bins[-1] if bins else None
        if (last is not None and weight <= share // 2 and last[1] <= share // 2
                and last[1] + weight <= share):
            bins[-1] = (last[0] + paths, last[1] + weight, min(last[2], depth))
        else:
            bins.append((list(paths), weight, depth))
    shards = []
    for index, (paths, weight, depth) in enumerate(bins):
        digest = hashlib.sha256('\n'.join(paths).encode()).hexdigest()[:8]
        shards.append(Shard(id=f'shard-{index:03d}-{digest}', paths=tuple(paths), readable_bytes=weight, depth=depth))
    return shards
