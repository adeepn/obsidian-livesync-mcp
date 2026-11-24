"""Reproducible local benchmark: PYTHONPATH=. venv/bin/python scripts/benchmark_selection.py MODE."""
import asyncio
import json
import resource
import statistics
import sys
import time
import tempfile

from device_selection import DeviceSelection
from selection_engine import evaluate


async def main(mode):
    backend = DeviceSelection()
    backend.selection_only = r'\.md$'
    directory = tempfile.TemporaryDirectory()
    if mode.startswith('journal-'):
        from journal_store import JournalStore
        backend._store = JournalStore(directory.name, 'legacy')
    samples = []
    for i in range(101):
        path = 'folder/cached.md' if mode.endswith('cache') else f'folder/{i}.md'
        start = time.perf_counter()
        if mode == 'one-shot':
            verdict = evaluate(backend._selection_payload(path))
        else:
            verdict = await backend.selection_for(path)
        assert verdict.allowed
        samples.append((time.perf_counter() - start) * 1000)
    await backend.selection_close()
    if getattr(backend, '_store', None):
        backend._store.close()
    directory.cleanup()
    # macOS reports bytes; Linux reports KiB. Child high-water RSS is collected
    # after wait(), so this includes Node rather than just the Python owner.
    scale = 1024 * 1024 if sys.platform == 'darwin' else 1024
    print(json.dumps(dict(mode=mode, samples=len(samples), cold_ms=samples[0],
        warm_median_ms=statistics.median(samples[1:]), warm_max_ms=max(samples[1:]),
        median_ms=statistics.median(samples), max_ms=max(samples),
        peak_child_rss_mib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / scale,
        parent_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale)))


asyncio.run(main(sys.argv[1]))
