"""Local deterministic splitter timings; run ./venv/bin/python scripts/benchmark_splitters.py."""
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from content_splitter import ContentSplitter

results = []
for version in ['v1', 'v2', 'v2-segmenter', 'v3-rabin-karp']:
    splitter = ContentSplitter({'chunkSplitterVersion': version})
    for shape in ['tiny-lines', 'huge-line', 'binary-no-delimiters']:
        for mib in [4, 8]:
            size = mib * 1024 * 1024
            data = ('x\n' * (size // 2) if shape == 'tiny-lines' else
                    'x' * size if shape == 'huge-line' else b'\xff' * size)
            elapsed = []
            for _ in range(3):
                start = time.perf_counter()
                pieces = splitter.split(data, path='x.md' if isinstance(data, str) else 'x.bin')
                elapsed.append(time.perf_counter() - start)
            results.append({'version': version, 'shape': shape, 'MiB': mib,
                            'median_seconds': round(statistics.median(elapsed), 6),
                            'runs_seconds': elapsed, 'pieces': len(pieces)})
print(json.dumps(results, indent=2))
