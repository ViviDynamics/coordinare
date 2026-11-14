"""Offline native evidence budgets, independent of provider/network latency."""
from __future__ import annotations

import hashlib
import json
import time
import tracemalloc
from statistics import median

import pytest

from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceSource, EvidenceStore

MIB = 1024 * 1024
PAGING_MS = 100
PEAK_BYTES = 20 * MIB


async def page_packet(content: str) -> int:
    source = EvidenceSource('nested/arbitrary-tool/source', content, hashlib.sha256(content.encode()).hexdigest())
    store = EvidenceStore((source,), required=frozenset({source.key}))
    delivered = 0
    while store.remaining_required:
        batch = await store.read(EvidenceRead())
        delivered += sum(len(page.content.encode()) for page in batch.pages)
    return delivered


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_four_mib_complete_paging_latency_and_memory() -> None:
    content = 'a' * (4 * MIB)
    samples = []
    for _ in range(5):
        started = time.perf_counter()
        assert await page_packet(content) == len(content)
        samples.append((time.perf_counter() - started) * 1000)
    tracemalloc.start()
    try:
        assert await page_packet(content) == len(content)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(json.dumps({'paging_median_ms': median(samples), 'paging_samples_ms': samples, 'peak_bytes': peak}))
    assert median(samples) <= PAGING_MS
    assert peak <= PEAK_BYTES


@pytest.mark.benchmark
def test_native_cold_start_against_same_runner_base() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    baseline = os.environ.get('COORDINARE_BENCHMARK_BASELINE')
    assert baseline, 'Supply the pinned PR-base source tree for the same-runner comparison'
    base_source = Path(baseline).resolve() / 'src'
    candidate_source = Path(__file__).resolve().parents[2].parent / 'src'
    assert (base_source / 'coordinare/daemon.py').is_file()
    samples: dict[str, list[float]] = {'base': [], 'candidate': []}
    code = 'import sys;sys.path.insert(0,sys.argv[1]);import coordinare.daemon'
    for repeat in range(5):
        paths = [('base', base_source), ('candidate', candidate_source)]
        for name, path in paths if repeat % 2 == 0 else reversed(paths):
            started = time.perf_counter()
            subprocess.run([sys.executable, '-I', '-c', code, str(path)], check=True, capture_output=True, timeout=30)
            samples[name].append(time.perf_counter() - started)
    base, candidate = median(samples['base']), median(samples['candidate'])
    print(json.dumps({'cold_start_base_seconds': base, 'cold_start_candidate_seconds': candidate, 'samples': samples}))
    assert candidate <= base * 1.1 + 0.1
