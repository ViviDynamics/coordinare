"""Offline native evidence budgets, independent of provider/network latency."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import time
import tracemalloc
from pathlib import Path
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


def baseline_source(repo: Path, destination: Path) -> tuple[Path, str]:
    """Use the supplied PR base, or archive a local immutable baseline offline."""
    configured = os.environ.get('COORDINARE_BENCHMARK_BASELINE')
    if configured:
        source = Path(configured).resolve() / 'src'
        assert (source / 'coordinare/daemon.py').is_file()
        return source, 'configured-checkout'

    def git(*args: str) -> str:
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True, timeout=30).strip()

    merge_base = subprocess.run(['git', '-C', str(repo), 'merge-base', 'HEAD', 'origin/main'],
                                capture_output=True, text=True, timeout=30, check=False)
    revision = merge_base.stdout.strip() if merge_base.returncode == 0 else ''
    if not revision or revision == git('rev-parse', 'HEAD'):
        # CI's PR merge checkout has the PR base as its first parent; main uses
        # its preceding commit. Both full-suite lanes retain that parent.
        revision = git('rev-parse', 'HEAD^')
    archive = subprocess.check_output(['git', '-C', str(repo), 'archive', revision, 'src'], timeout=30)
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(destination, filter='data')
    source = destination / 'src'
    assert (source / 'coordinare/daemon.py').is_file()
    return source, revision


@pytest.mark.benchmark
def test_native_cold_start_against_same_runner_base(tmp_path: Path) -> None:
    import sys

    repo = Path(__file__).resolve().parents[2]
    base_source, baseline_revision = baseline_source(repo, tmp_path / 'baseline')
    candidate_source = repo / 'src'
    samples: dict[str, list[float]] = {'base': [], 'candidate': []}
    code = ('import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import coordinare.daemon;'
            'assert Path(coordinare.daemon.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())')
    for repeat in range(5):
        paths = [('base', base_source), ('candidate', candidate_source)]
        for name, path in paths if repeat % 2 == 0 else reversed(paths):
            started = time.perf_counter()
            subprocess.run([sys.executable, '-I', '-c', code, str(path)], check=True, capture_output=True, timeout=30)
            samples[name].append(time.perf_counter() - started)
    base, candidate = median(samples['base']), median(samples['candidate'])
    print(json.dumps({'baseline_revision': baseline_revision, 'cold_start_base_seconds': base, 'cold_start_candidate_seconds': candidate, 'samples': samples}))
    assert candidate <= base * 1.1 + 0.1
