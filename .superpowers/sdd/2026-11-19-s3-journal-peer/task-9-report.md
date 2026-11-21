## Task 9 — env-gated MinIO integration

Status: implemented; live MinIO validation is reserved for the controller.

- Added `tests/test_integration_minio.py` with four scenarios: five-note NFC/Cyrillic/frontmatter roundtrip, prefix wipe plus changed protocol epoch, interrupted second journal with durable first-file checkpoint and restart, and independent sibling writers with both revision bodies and unrelated notes retained.
- Reuses the Task 4 environment gate and legacy credential aliases. Each test uses a UUID prefix and independent local peer directories. Harness-only boto3 administration seeds control objects and deletes/reads back the test prefix during cleanup.
- Backend protocol calls are recorded; deletion fails immediately at the proxy and at the underlying boto3 client event hook. Teardown asserts zero deletion attempts. The fixed sibling allocator clock is scoped to the backend module so boto3 signing retains the real clock.
- No Docker, network service, or env-enabled integration test was executed by this session. The controller will run the suite with MinIO environment variables and save the actual live output to `docs/test-artifacts/2026-11-19-minio-integration.txt`; that live artifact is pending and has not been fabricated.
- Local verification explicitly unsets all six canonical/alias MinIO environment variables: `./venv/bin/python -m pytest tests/ -q`. Output: `835 passed, 7 skipped, 3 subtests passed`. Four new skips are the integration scenarios. Durable output: `docs/test-artifacts/2026-11-19-minio-integration-local.txt`.
- Reviewed the complete new test diff and ran `git diff --check`. Concern: live service behavior remains unverified until the controller run. No production source changes.
