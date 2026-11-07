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
MIN_SPLIT = 2
_Tree = tuple[dict[str, list['InventoryEntry']], dict[str, list[str]], dict[str, int], dict[str, int]]


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


def _units(directory: str, tree: _Tree, limits: tuple[int, int | None]) -> list[tuple[list[str], int, int]]:
    """Each unit fits the share and file bound: a whole directory, or the split units of an oversized one."""
    share, max_files = limits
    files, dirs, totals, counts = tree
    depth = directory.count('/') + 1 if directory else 0
    children: list[tuple[str, list[tuple[list[str], int, int]]]] = [
        (entry.path, [([entry.path], shard_weight(entry), depth)]) for entry in files.get(directory, [])]
    for child in dirs.get(directory, []):
        if totals[child] <= share and (max_files is None or counts[child] <= max_files):
            children.append((child, [(_collect(child, files, dirs), totals[child], child.count('/') + 1)]))
        else:
            children.append((child, _units(child, tree, limits)))
    return [unit for _name, units in sorted(children, key=lambda item: item[0]) for unit in units]


def _collect(directory: str, files: dict[str, list[InventoryEntry]], dirs: dict[str, list[str]]) -> list[str]:
    paths = [entry.path for entry in files.get(directory, [])]
    for child in dirs.get(directory, []):
        paths.extend(_collect(child, files, dirs))
    return sorted(paths)


def plan_shards(entries: Mapping[str, InventoryEntry], *, share: int, spine: Collection[str],
                max_file_bytes: int, max_files: int | None = None) -> list[Shard]:
    """Recursive, deterministic split: fitting directories stay whole, small neighbours pack together.

    A unit over half the share stands alone, so one large file never crowds out its siblings. max_files keeps
    each shard within one conversation's read allowance.
    """
    excluded = set(spine)
    files: dict[str, list[InventoryEntry]] = {}
    dirs: dict[str, set[str]] = {}
    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    for path in sorted(entries):
        entry = entries[path]
        if path in excluded or not readable(entry, max_file_bytes=max_file_bytes):
            continue
        parts = path.split('/')
        files.setdefault('/'.join(parts[:-1]), []).append(entry)
        for index in range(len(parts) - 1):
            ancestor = '/'.join(parts[:index + 1])
            dirs.setdefault('/'.join(parts[:index]), set()).add(ancestor)
            totals[ancestor] = totals.get(ancestor, 0) + shard_weight(entry)
            counts[ancestor] = counts.get(ancestor, 0) + 1
    tree = (files, {key: sorted(value) for key, value in dirs.items()}, totals, counts)
    bins: list[tuple[list[str], int, int]] = []
    for paths, weight, depth in _units('', tree, (share, max_files)):
        last = bins[-1] if bins else None
        if (last is not None and weight <= share // 2 and last[1] <= share // 2 and last[1] + weight <= share
                and (max_files is None or len(last[0]) + len(paths) <= max_files)):
            bins[-1] = (last[0] + paths, last[1] + weight, min(last[2], depth))
        else:
            bins.append((list(paths), weight, depth))
    shards = []
    for index, (paths, weight, depth) in enumerate(bins):
        digest = hashlib.sha256('\n'.join(paths).encode()).hexdigest()[:8]
        shards.append(Shard(id=f'shard-{index:03d}-{digest}', paths=tuple(paths), readable_bytes=weight, depth=depth))
    return shards


def split_by_bytes(shard: Shard, entries: Mapping[str, InventoryEntry]) -> list[Shard]:
    """Two children of roughly equal readable bytes; a single file cannot split further."""
    if len(shard.paths) < MIN_SPLIT:
        return [shard]
    weights = [shard_weight(entries[path]) if path in entries else 0 for path in shard.paths]
    half, running, cut = sum(weights) / 2, 0, 1
    for index, weight in enumerate(weights[:-1], start=1):
        running += weight
        cut = index
        if running >= half:
            break
    halves = (shard.paths[:cut], shard.paths[cut:])
    return [Shard(id=f'{shard.id}.{index}', paths=paths, readable_bytes=sum(shard_weight(entries[p]) for p in paths if p in entries),
                  depth=shard.depth + 1) for index, paths in enumerate(halves)]
