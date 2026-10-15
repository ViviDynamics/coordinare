"""Spec 234 review: ctime-only churn is refreshed by a content check, and the payload is reused.

The dispatch stat pass compares one stat digest. A mismatch falls to the content check, which streams the manifest.
A content check that passes refreshes the stat digest, so the next dispatch is stat-only again. A content change after
a refresh is still refused. The consumer payload is built once per digest and reused.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from tests.unit.test_234_honest_path import _bootstrapped, _service, _state_map


def test_a_ctime_bump_is_refreshed_and_the_next_dispatch_is_stat_only(tmp_path: Path) -> None:
    state, cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state)))
    first, _ = asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert first.ok and first.hashed, "the first dispatch after a freeze runs the content check"

    os.chmod(cache / "bin" / "ruby", 0o755)  # ctime moves, the bytes do not
    churned, _ = asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert churned.ok, churned.reason
    assert churned.hashed, "the stat digest mismatch falls to the content check, which refreshes it"

    settled, _ = asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert settled.ok, settled.reason
    assert settled.hashed is False, "after the refresh the dispatch stat pass is clean"


def test_a_content_change_after_the_refresh_is_still_refused(tmp_path: Path) -> None:
    state, cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state)))
    asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    os.chmod(cache / "bin" / "ruby", 0o755)
    asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))  # refreshed

    tool = cache / "bin" / "ruby"
    st = tool.stat()
    tool.write_bytes(tool.read_bytes().replace(b"ruby", b"RUBY"))  # same size, different bytes
    os.utime(tool, ns=(st.st_atime_ns, st.st_mtime_ns))
    check, payload = asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert check.ok is False
    assert payload is None
    assert "ruby" in (check.reason or "")


def test_the_payload_is_built_once_per_digest_and_matches_it(tmp_path: Path) -> None:
    state, _cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state)))
    check1, payload1 = asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    check2, payload2 = asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert check1.ok and check2.ok
    assert payload1 is payload2, "the second dispatch reuses the small payload"
    assert payload1 is not None and payload1["digest"] == state.provenance_digest


def test_resolve_refuses_when_nothing_is_frozen(tmp_path: Path) -> None:
    state, _cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    check, payload = asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert check.ok is False
    assert payload is None
