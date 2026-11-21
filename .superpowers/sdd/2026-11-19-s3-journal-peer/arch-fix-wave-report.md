
## Final combined architecture fix wave — 2026-11-21

Status: COMPLETE offline; live MinIO rerun belongs to controller.

### Premises
- Publication previously uploaded before codec/store validation; an oversized compact line could become an immutable replay blocker.
- Failed refresh armed the throttle without preserving its error.
- Integration StorageSpy lacked bounded GET and conditional publication; module-wide skip concealed protocol drift.
- Authority text still described full listings, winner-only chunk retention, exists-first publication and journal RenamePartialError.

### Execution trace
1. Publication serializes, checks inflated byte size, parses with configured record/line limits, validates store chunk/revision constraints without mutation, compresses, checks object/download caps, then probes wipe and publishes. Read-back uses the same configured codec limits before local apply.
2. Refresh preserves the last exception. A throttled unresolved failure raises StorageError with retry guidance and chained cause. A full successful attempt clears it; force bypasses the throttle. Partial budget stops do not erase an unresolved failure.
3. StorageSpy forwards max_bytes and put_if_absent, recording conditional publication. A non-env-gated contract test checks signatures and exercises bounded GET, put, conditional put, list and exists. Only four live tests retain the MinIO skip.
4. Spec amendments preserve superseded wording and name the ruling; Task 7 states single-file atomic journal rename and CouchDB-only RenamePartialError. README explicitly describes errors during failure backoff.

### Conclusions and evidence
- Red regression run: 9 expected failures (arch-wave-red.txt); offline spy red: 1 failure, 4 skips (arch-spy-red.txt).
- Focused preflight/store: 188 passed (arch-preflight-green.txt). Focused backend after backoff: 139 passed (arch-backoff-green.txt). Offline wrapper: 1 passed, 4 skipped (arch-spy-green.txt).
- Initial full run exposed the second outdated silent-backoff assertion in test_journal_security.py; updated it to require StorageError while preserving the no-network assertion.
- Final full suite: **881 passed, 7 skipped**, 12.25s (arch-wave-pytest.txt). Current pytest output does not report a separate subtest count.
- Python compilation exit 0 (arch-wave-build.json); git diff --check passed.
- Oversized >64 MiB body and each configured limit reject before fake put calls; invalid store revision also publishes nothing. Normal write coverage remains green.
- No external storage or live infrastructure used. Seven env-gated tests remain skipped.

### Rejected paths
- Upload then validate: rejected because immutable bad objects wedge replay.
- Clear failure on throttle/budget stop: rejected because stale reads would resume without a successful refresh.
- Keep offline contract behind global skip: rejected because protocol drift would remain invisible.
- Delete historical chunks or remote journals: out of scope and contrary to final retention/publication semantics.

### Commits

- c88a2fa docs: reconcile spec with final journal semantics
- bb10b59 test: repair minio spy contract
- cce0ac1 fix: surface refresh failures during backoff
- aadfa1e fix: preflight publication limits

- 2026-11-21 follow-up 4513634: MinIO scenarios count successful put_if_absent and fallback put creations exactly once per key, record conditional results and reject plain-put overwrites; offline test_integration_minio.py: 1 passed, 4 skipped (arch-minio-publications-offline.txt); live rerun remains with controller.
- 2026-11-21 round 2: reserve the last observed sync-parameters size plus 1 KiB (64 KiB when unknown) before publication; zero-write refusal and fresh-reader boundary replay covered; full suite: 885 passed, 7 skipped, 3 subtests passed (arch-control-reserve-pytest.txt); compilation and diff checks passed.
