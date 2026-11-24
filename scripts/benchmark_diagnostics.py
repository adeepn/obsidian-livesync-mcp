"""Measure capped last-line redaction without timing input allocation."""
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diagnostic_hygiene import sanitize_diagnostic


def main():
    results = []
    for size in (1024, 100_000, 1_000_000):
        for shape, unit in [('letters', 'a'), ('dots', '.'), ('percent', '%25'),
                            ('nested', '{"password":'), ('url-at-end', 'a')]:
            value = (unit * (size // len(unit) + 1))[:size]
            if shape == 'url-at-end':
                value += 'https://user:synthetic-marker@host'
            timings = []
            for _ in range(30):
                start = time.perf_counter_ns()
                sanitize_diagnostic(value)
                timings.append((time.perf_counter_ns() - start) / 1_000_000)
            results.append(dict(size=size, shape=shape,
                median_ms=statistics.median(timings), max_ms=max(timings)))
    print(json.dumps(results, indent=2))
    return int(any(r['max_ms'] >= 50 for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
