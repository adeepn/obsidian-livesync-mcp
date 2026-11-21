
## Final-review fix wave — 2026-11-21

Branch: `plan-b-s3-journal`. Scope: two Important and two Minor findings.

### Premises

- `storage_journal.py`: startup owned the only epoch comparison and last-sent wipe probe; refresh filtered processed keys before any runtime reset.
- `_as_ms` converted floats with `int()` and silently defaulted malformed types; metadata construction precedes per-note text reads. Existing listing/scan metadata guards catch `StorageError`.

### Execution trace

1. Refresh lists all journals, GETs sync-parameters once, resets on epoch mismatch, probes a configured last-sent key, and only then filters processed keys and replays. Reset clears the throttle and preserves the current epoch.
2. Publication probes the last-sent object before PUT. A missing object resets state and raises `ConcurrentModification("remote was rebuilt; retry the operation after refresh")`; the attempted write publishes nothing.
3. Timestamp validation raises `StorageError` for non-numeric, boolean, non-finite, or negative values. Missing legacy timestamps remain zero. Existing metadata guards skip corrupt notes in listing and text scans; healthy notes remain available.
4. Spec §4.4 now records full listing (B-8), random 128-bit key components and collision checks (B-10), raw DEFLATE RFC 1951 (B-4), and advisory locking (B-11). Task 10 and checklist accept screenshots OR verbatim client logs (B-12), including the committed client transcript.

### Conclusions and evidence

- Added eight regression cases: runtime wipe convergence, runtime epoch convergence without another startup, refusal of a stale write with no PUT, and five malformed timestamp cases (including JSON `1e999` and string ctime).
- RED: `final-fix-wave-red.txt` — all eight new cases fail against the original implementation.
- Focused: `final-fix-wave-focused.txt` — 130 passed.
- Full: `final-fix-wave-pytest.txt` — command `./venv/bin/python -m pytest tests/ -q`; final result recorded below after completion.
- Read the code/test/doc diffs; `git diff --check` passes.

### Rejected paths / limits

- Re-running startup is not a runtime guard; regression cases initialize once.
- Filtering processed keys before reset would miss replacement objects reusing old names; tests explicitly replace an already processed key.
- Catching only body-read errors would leave metadata failures outside isolation; validation uses the existing metadata `StorageError` boundary.
- No live bucket or Obsidian acceptance was rerun in this wave. B-12 updates the evidence contract; it does not invent new client evidence.

Final verification: **844 passed, 7 skipped, 3 subtests passed in 51.14s**, exit 0.
Baseline retained (836 passed); eight regression cases added. All four findings resolved.
