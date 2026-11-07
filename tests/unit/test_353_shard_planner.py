"""Size-based source shards come only from pinned paths, sizes and modes."""
from __future__ import annotations

from coordinare_service_inference.environment.inventory import InventoryEntry

from coordinare.services.environment_shards import (
    plan_shards,
    shard_weight,
    share_bytes,
    spine_paths,
)

LIMIT = 256 * 1024


def entries(sizes: dict[str, int], links=()):
    data = {path: InventoryEntry(path, size, '100644') for path, size in sizes.items()}
    data.update({path: InventoryEntry(path, 9, '120000', target='elsewhere') for path in links})
    return data


def covered(shards):
    return [path for shard in shards for path in shard.paths]


def test_share_is_half_the_context_at_three_and_a_half_bytes_per_token():
    assert share_bytes(262_144) == int(262_144 * 0.5 * 3.5)


def test_shards_cover_every_readable_path_exactly_once():
    inventory = entries({f'pkg{d}/mod{i}.py': 40_000 for d in range(5) for i in range(12)} | {'README.md': 2_000})
    shards = plan_shards(inventory, share=200_000, spine=['README.md'], max_file_bytes=LIMIT)
    paths = covered(shards)
    assert len(paths) == len(set(paths)) and set(paths) == set(inventory) - {'README.md'}


def test_shards_respect_share():
    inventory = entries({f'a/b{d}/f{i}': 30_000 for d in range(6) for i in range(10)})
    for shard in plan_shards(inventory, share=100_000, spine=[], max_file_bytes=LIMIT):
        assert sum(shard_weight(inventory[path]) for path in shard.paths) <= 100_000


def test_large_directory_splits_recursively():
    inventory = entries({f'src/deep/er/f{i}': 50_000 for i in range(10)})
    shards = plan_shards(inventory, share=120_000, spine=[], max_file_bytes=LIMIT)
    assert len(shards) >= 5 and all(shard.depth >= 3 for shard in shards)


def test_small_siblings_pack():
    inventory = entries({f'd{i}/f': 1_000 for i in range(20)})
    assert len(plan_shards(inventory, share=100_000, spine=[], max_file_bytes=LIMIT)) == 1


def test_one_big_file_among_many_small():
    sizes = {f'lib/small{i}': 500 for i in range(100)} | {'lib/huge': 200_000}
    shards = plan_shards(entries(sizes), share=210_000, spine=[], max_file_bytes=LIMIT)
    [huge] = [shard for shard in shards if 'lib/huge' in shard.paths]
    assert huge.paths == ('lib/huge',)
    assert sum(len(shard.paths) for shard in shards if shard is not huge) == 100


def test_unreadable_paths_listed_not_sharded():
    inventory = entries({'big.bin': LIMIT + 1, 'ok.txt': 10}, links=['link'])
    assert covered(plan_shards(inventory, share=100_000, spine=[], max_file_bytes=LIMIT)) == ['ok.txt']


def test_plan_is_deterministic():
    inventory = entries({f'x{i % 7}/y{i}': 10_000 + i for i in range(80)})
    first = plan_shards(inventory, share=90_000, spine=[], max_file_bytes=LIMIT)
    second = plan_shards(dict(reversed(list(inventory.items()))), share=90_000, spine=[], max_file_bytes=LIMIT)
    assert first == second and len({shard.id for shard in first}) == len(first)


def test_spine_is_root_only_and_bounded():
    inventory = entries({'README.md': 3_000, 'Makefile': 1_000, 'huge.md': 90_000, 'docs/guide.md': 10},
                        links=['root-link'])
    assert spine_paths(inventory, limit_bytes=10_000, max_file_bytes=LIMIT) == ['Makefile', 'README.md']
