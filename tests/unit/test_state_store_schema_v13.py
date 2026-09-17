"""Schema v13 — OpenWiki wiki-init fields on EnvCacheStateSnapshot (spec 124 US3)."""
from __future__ import annotations

from pathlib import Path

from coordinare.daemon import _persist_env_cache
from coordinare.models.env_cache import EnvCacheState
from coordinare.state_store import CURRENT_SCHEMA_VERSION, EnvCacheStateSnapshot


def test_schema_version_bumped_to_13() -> None:
    # 124 bumped to v13; later specs bump further (125 → v14), and the v13 wiki
    # fields remain supported — so assert >= 13, not an exact pin.
    assert CURRENT_SCHEMA_VERSION >= 13


def test_old_snapshot_loads_wiki_fields_with_safe_defaults() -> None:
    # A v12-shaped snapshot (no wiki fields) must load with defaults — no migration.
    snap = EnvCacheStateSnapshot.model_validate(
        {"symphony_name": "s", "sanitised_name": "s", "cache_dir": "/tmp/c"},
    )
    assert snap.wiki_initialized is False
    assert snap.wiki_attempts == 0
    assert snap.wiki_exhausted is False
    assert snap.last_wiki_init_at is None
    assert snap.last_wiki_init_succeeded is None
    assert snap.last_wiki_init_error is None


def test_wiki_fields_round_trip_through_snapshot() -> None:
    snap = EnvCacheStateSnapshot(
        symphony_name="s", sanitised_name="s", cache_dir="/tmp/c",
        wiki_initialized=True, wiki_attempts=2, wiki_exhausted=True,
    )
    snap2 = EnvCacheStateSnapshot.model_validate(snap.model_dump())
    assert snap2.wiki_initialized is True
    assert snap2.wiki_attempts == 2
    assert snap2.wiki_exhausted is True


def test_persist_env_cache_carries_wiki_fields_but_not_transient_in_flight() -> None:
    ec = EnvCacheState(
        symphony_name="s", sanitised_name="s", cache_dir=Path("/tmp/c"),
        wiki_initialized=True, wiki_attempts=1, wiki_in_flight=True,
    )
    out = _persist_env_cache({"s": ec})
    assert out["s"].wiki_initialized is True
    assert out["s"].wiki_attempts == 1
    # wiki_in_flight is transient — not a field on the snapshot at all.
    assert not hasattr(out["s"], "wiki_in_flight")
