# Task 10 — live-client acceptance package (2026-11-20)

Status: package complete; live-client acceptance pending user + controller.

- Added scripts/live_check_s3.py: sweep-notes, verify-vault, write-phase,
  verify-visible (--path repeatable). Each command constructs JournalBackend,
  runs startup_check and forced refresh, closes backend, emits PASS/FAIL and
  returns nonzero on failure. Uses repository imports with venv Python.
- Sweep captures the first chronological journal's raw bytes as the golden
  fixture. No synthetic fixture supplied. Missing fixture skips codec test.
- Verify compares UTF-8 re-encoded text / binary bytes against local md/png
  files outside hidden directories. Empty selection fails. verify-visible
  verifies the named client's file after forced refresh.
- Write phase creates Cyrillic paths, appends with expected_rev, moves and
  soft-deletes; reports revisions (delete revision decoded from publication)
  and reads back all resulting states. Preflight prevents normal reruns;
  partial failures require inspection, not blind retries.
- Added Russian checklist with current SeaweedFS endpoint and 2026-11-20
  artifacts directory, LEGACY identity unchanged, screenshot requirement,
  client-write round trip and plan-Д explicitly deferred.
- .gitignore already permits tests/fixtures/*.bin; git check-ignore returned 1
  for the future fixture. No ignore change required.

Verification:
- venv/bin/python -m pytest: 835 passed, 8 skipped (baseline + missing golden).
- compileall and CLI --help passed; git diff --check passed.
- Offline helper check on actual JournalBackend + FakeObjectStorage (LEGACY)
  passed create/append/move/delete/read-back, rerun refusal, exact-match,
  mismatch and empty-selection cases. Expected FAIL lines are labeled in
  the artifacts README.
- Durable output: docs/test-artifacts/2026-11-20-live-s3/preparation-pytest.txt
  and preparation-offline.txt. No real S3/vault access or client operations.

Concerns / remaining acceptance:
- Client acceptance of the unregistered writer remains unverified; collect
  LiveSync log screenshot and follow plan-Д separately if rejected.
- First journal must include >=1 doc for the requested golden assertion;
  sweep captures it but fails explicitly if it contains only compact chunks.
- Brief's old MinIO/date/execution steps were superseded by the explicit
  package-only request using existing SeaweedFS. No identity migration.

## RULING B-11 — advisory locked milestone (2026-11-20)

- [DECISION] Accept `locked: true` at startup: upstream treats the flag as
  advisory and plugin 1.0.50 leaves it after a completed Rebuild. The joining
  peer performs full replay (fetch-equivalent); log WARNING naming the flag
  and stating "remote marked as rebuilt; peer performs full replay".
- Replace the locked-refusal test with startup acceptance and caplog WARNING
  assertions. All other refusal paths remain unchanged, including journals
  without a milestone. Align design spec section 4.4; README has no
  locked-refusal statement requiring an update.
- Red check: the new acceptance test failed on the previous UnsupportedVault.
- Verification: `./venv/bin/python -m pytest tests/ -q` returned exit 0:
  **835 passed, 8 skipped, 3 subtests passed** (9.75s). No baseline regressions.
  Full output: `docs/test-artifacts/2026-11-20-live-s3/b11-pytest.txt`.
